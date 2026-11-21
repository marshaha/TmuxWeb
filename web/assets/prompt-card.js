/* 聊天里的「交互菜单」卡片：把 pane 里正在等的选择搬进聊天，不用切终端。
 *
 * 场景：kimi 的 /model 选择器、claude 的「Do you want to proceed?」、kimi 的
 * 「1. Compact and continue」—— 这些是 TUI 占满屏幕画的，聊天里原本只看到
 * "已发送"。服务端从抓屏里解析出来（agent_prompts.py），这里渲染成按钮。
 *
 * 原则：
 *   - 默认不自动应答：只有人点了才发键；
 *   - 唯一的例外是人自己勾了「记住这个选择」——此后**同一道题**（标题+选项的指纹相同）
 *     才会被自动答，作用域是本轮或一直，可随时撤销，并在卡片上写明"它替你点了什么"；
 *     指纹不同、换了一轮（turn 作用域）、或授权过期，都退回手点；
 *   - 卡片里给屏幕原文，用户能核对自己在答哪个问题；
 *   - 认不出/没把握就不弹（服务端返回 null），随时可以切终端；
 *   - 键盘只在卡片聚焦时接管，输入框打字不受影响。
 *
 *   TmuxPrompt.start(() => state.selected)   // 开轮询
 *   TmuxPrompt.stop()
 *   TmuxPrompt.refresh()                    // 切 pane 后立刻查一次
 */
(function () {
  const POLL_MS = 2500;
  const CARD = 'prompt-card';

  let getPane = null;
  let timer = null;
  let busy = false;
  let busyTimer = 0;
  // busy 只是防重复提交的锁，不是"永远别再点"的状态：请求万一挂着没回来，
  // 8 秒后自动放开，否则卡片会一直回"上一条还在发送"、看着像坏了。
  const BUSY_MAX_MS = 8000;
  function lockBusy() {
    busy = true;
    clearTimeout(busyTimer);
    busyTimer = setTimeout(() => { busy = false; }, BUSY_MAX_MS);
  }
  function unlockBusy() {
    busy = false;
    clearTimeout(busyTimer);
    busyTimer = 0;
  }
  let prompt = null;
  let cursor = 0;
  let hiddenByUser = false;      // 用户点了 ✕：这一轮别再弹
  // 乐观关闭：点下去卡片先收起来，请求在后台跑；失败了卡片恢复出来、显示
  // 原因，可以再点一次。以前卡片一直杵在“已发送（等屏幕刷新…）”上等 POST
  // + 跟进轮询，手机上走 Cloudflare 要好几秒，看着像卡住了。
  // 答过的题记一个签名：sweep 更新之前 poll 别再把它弹出来（和 needs-you.js
  // 的 answeredSignature 一个意思）；失败恢复时签名清掉，不然没法重试。
  let answeredSignature = '';
  let lastSignature = '';
  let rememberChoice = false;
  let rememberScope = 'turn';
  let autoAnswered = '';
  let answering = false;       // 手里正有一条选择在路上（poll 自己的 busy 不能用来判断这个）
  let lastScreen = [];

  const $ = id => document.getElementById(CARD + '-' + id);

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  }

  function paneNow() {
    try { return getPane ? getPane() : ''; } catch (_error) { return ''; }
  }

  function card() { return document.getElementById(CARD); }

  function signatureOf(found) {
    if (!found) return '';
    // 和 needs-you.js 同款形状：标题 + 选项 + 开关当前值 + 光标，描述不进签名。
    return JSON.stringify([
      found.kind || '', found.title || '',
      (found.options || []).map(option => [option.label, option.meta, option.number]),
      (found.toggles || []).map(toggle => [toggle.label, toggle.active]),
      found.selected,
    ]);
  }

  function turnKey() {
    try { return (window.TmuxTurn && window.TmuxTurn.key()) || ''; } catch (_error) { return ''; }
  }

  function postJson(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    }).then(response => response.json().catch(() => ({})));
  }

  // 记住这次选择（可选）。没勾就不记 —— 自动答题必须是人明确同意的。
  function rememberRow(data) {
    const row = document.createElement('div');
    row.className = 'prompt-remember';
    const label = document.createElement('label');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.id = 'remember-box';
    box.checked = !!rememberChoice;
    box.addEventListener('change', () => { rememberChoice = box.checked; });
    label.appendChild(box);
    const labelText = document.createElement('span');
    labelText.textContent = '记住这个选择';
    label.appendChild(labelText);
    const scope = document.createElement('select');
    scope.id = 'remember-scope';
    for (const [value, text] of [['turn', '本轮'], ['always', '一直']]) {
      const option = document.createElement('option');
      option.value = value; option.textContent = text;
      scope.appendChild(option);
    }
    scope.value = rememberScope;
    scope.addEventListener('change', () => { rememberScope = scope.value; });
    row.appendChild(label);
    row.appendChild(scope);
    const note = document.createElement('span');
    note.className = 'prompt-remember-note';
    note.id = 'remember-note';
    row.appendChild(note);
    if (data && data.grant) {
      const grant = data.grant;
      note.textContent = `已授权：${grant.label || '这个选择'}（${grant.scope === 'always' ? '一直' : '本轮'}，用过 ${grant.uses} 次）`;
      const undo = document.createElement('button');
      undo.type = 'button';
      undo.className = 'prompt-remember-undo';
      undo.textContent = '撤销';
      undo.addEventListener('click', async () => {
        await fetch(`/api/panes/${encodeURIComponent(paneNow())}/approvals/${encodeURIComponent(grant.id)}`,
          {method: 'DELETE', credentials: 'same-origin'});
        note.textContent = '已撤销授权';
      });
      row.appendChild(undo);
    }
    return row;
  }

  async function rememberIfAsked(action, label) {
    if (!rememberChoice || !lastSignature) return false;
    try {
      await postJson(`/api/panes/${encodeURIComponent(paneNow())}/approvals`, {
        signature: lastSignature, action, scope: rememberScope, turn: turnKey(), label,
      });
      return true;
    } catch (_error) {
      return false;
    }
  }

  function hide() {
    const node = card();
    if (node) node.hidden = true;
    prompt = null;
    cursor = 0;
    hiddenByUser = false;
  }

  function paintCursor() {
    const list = $('options');
    if (!list || !prompt || !prompt.options || !prompt.options.length) return;
    [...list.children].forEach((node, index) => {
      node.classList.toggle('on', index === cursor);
      node.setAttribute('aria-selected', index === cursor ? 'true' : 'false');
    });
  }

  function render(data, screen, signature) {
    const node = card();
    if (!node) return;
    prompt = data;
    cursor = Number.isInteger(data.selected) ? Math.min(data.selected, data.options.length - 1) : 0;

    if (signature !== undefined) lastSignature = signature || '';
    $('title').textContent = data.title || (data.kind === 'confirm' ? '需要确认' : '等待选择');
    const hint = $('hint');
    hint.textContent = data.hint || (data.more ? `屏幕外还有 ${data.more} 项` : '');
    hint.hidden = !hint.textContent;

    const list = $('options');
    list.innerHTML = '';
    data.options.forEach((option, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'prompt-option';
      button.setAttribute('role', 'option');
      button.dataset.index = String(index);
      const label = document.createElement('span');
      label.className = 'prompt-label';
      label.textContent = (option.number ? option.number + '. ' : '') + (option.label || '');
      button.appendChild(label);
      if (option.meta) {
        const meta = document.createElement('span');
        meta.className = 'prompt-meta';
        meta.textContent = option.meta;
        button.appendChild(meta);
      }
      button.addEventListener('click', () => chooseByIndex(index));
      list.appendChild(button);
    });

    const toggles = $('toggles');
    toggles.innerHTML = '';
    (data.toggles || []).forEach((toggle, toggleIndex) => {
      const row = document.createElement('div');
      row.className = 'prompt-toggle';
      const label = document.createElement('span');
      label.className = 'prompt-toggle-label';
      label.textContent = toggle.label || '开关';
      row.appendChild(label);
      const group = document.createElement('div');
      group.className = 'prompt-toggle-group';
      toggle.options.forEach((option, valueIndex) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'prompt-toggle-btn' + (valueIndex === toggle.active ? ' on' : '');
        button.textContent = option;
        button.setAttribute('aria-pressed', valueIndex === toggle.active ? 'true' : 'false');
        button.addEventListener('click', () => chooseToggle(toggleIndex, valueIndex));
        group.appendChild(button);
      });
      row.appendChild(group);
      if (toggle.hint) {
        const hint = document.createElement('span');
        hint.className = 'prompt-toggle-hint';
        hint.textContent = toggle.hint;
        row.appendChild(hint);
      }
      toggles.appendChild(row);
    });
    toggles.hidden = !(data.toggles && data.toggles.length);

    // 记住这次选择 —— 只有勾上才会写授权，勾了才可能被自动答题。
    const oldRow = document.getElementById('remember-row');
    if (oldRow && oldRow.remove) oldRow.remove();
    const remember = rememberRow(data);
    remember.id = 'remember-row';
    const anchor = toggles.hidden ? list : toggles;
    if (anchor.after) anchor.after(remember);
    else if (anchor.parentNode) anchor.parentNode.appendChild(remember);

    lastScreen = screen || [];
    // 手机上没有回车键。开关类界面（kimi 的 Thinking：←→ 选、Enter 确认）和提示里
    // 明写 confirm/继续/continue 的，给一颗「确认 Enter」按钮，点它等于敲回车。
    const enterButton = $('enter');
    if (enterButton) {
      const hint = String(data.hint || '');
      const needsEnter = data.kind === 'toggle'
        || /press enter/i.test(hint)
        || (/confirm|continue|确认|继续/i.test(hint) && !(data.options || []).length);
      enterButton.hidden = !needsEnter;
    }
    const raw = $('raw');
    if (raw) raw.textContent = (screen || []).join('\n');
    const details = $('raw-wrap');
    if (details) details.hidden = !screen || !screen.length;

    node.hidden = false;
    hiddenByUser = false;
    paintCursor();
  }

  async function post(url, body) {
    const response = await fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    return {ok: response.ok, status: response.status, data};
  }

  async function chooseByIndex(index) {
    const pane = paneNow();
    if (!pane) return;
    const chosen = prompt && prompt.options && prompt.options[index];
    // 不 await：点下就要立刻收卡（这是原来就有的体验），记授权在后台跑
    void rememberIfAsked({index}, (chosen && chosen.label) || '');
    if (busy) { setState('上一条还在发送，稍等一下…'); return; }   // 别静默吞掉点击
    // 乐观关闭：点下的一刻就收卡，请求在后台跑（和 needs-you.js 一个做法）。
    const saved = prompt;
    const savedScreen = lastScreen;
    answeredSignature = signatureOf(prompt);
    hide();
    setState('');
    lockBusy();
    answering = true;
    try {
      const result = await post(`/api/panes/${encodeURIComponent(pane)}/prompt/choose`, {index});
      if (!result.ok) {
        answeredSignature = '';
        if (saved) render(saved, savedScreen);
        setState(result.data.detail || `HTTP ${result.status}`, true);
        return;
      }
      setState('');
      unlockBusy();                     // 先放开锁：跟进 poll 靠它判断
      setTimeout(poll, 1500);           // 下一轮 poll 前先看一眼
    } catch (error) {
      answeredSignature = '';
      if (saved) render(saved, savedScreen);
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      answering = false;
      unlockBusy();
    }
  }

  async function chooseToggle(toggleIndex, value) {
    void rememberIfAsked({toggle: [toggleIndex, value]}, `开关 ${toggleIndex} = ${value}`);
    const pane = paneNow();
    if (!pane) return;
    if (busy) { setState('上一条还在发送，稍等一下…'); return; }
    if (prompt && prompt.toggles && prompt.toggles[toggleIndex]
        && prompt.toggles[toggleIndex].active === value) {
      setState('已经是这个值了');
      return;
    }
    const saved = prompt;
    const savedScreen = lastScreen;
    answeredSignature = signatureOf(prompt);
    hide();
    setState('');
    lockBusy();
    answering = true;
    try {
      const result = await post(`/api/panes/${encodeURIComponent(pane)}/prompt/choose`,
        {toggle: toggleIndex, value});
      if (!result.ok) {
        answeredSignature = '';
        if (saved) render(saved, savedScreen);
        setState(result.data.detail || `HTTP ${result.status}`, true);
        return;
      }
      if (result.data.unchanged) {
        // 服务端说值没变：菜单还在，把卡片恢复出来说清楚
        answeredSignature = '';
        if (saved) render(saved, savedScreen);
        setState('已经是这个值了');
        return;
      }
      setState('');
      unlockBusy();                     // 先放开锁：跟进 poll 靠它判断
      setTimeout(poll, 1500);
    } catch (error) {
      answeredSignature = '';
      if (saved) render(saved, savedScreen);
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      answering = false;
      unlockBusy();
    }
  }

  async function chooseByKey(key) {
    const pane = paneNow();
    if (!pane) return;
    if (busy) { setState('上一条还在发送，稍等一下…'); return; }
    const saved = prompt;
    const savedScreen = lastScreen;
    answeredSignature = signatureOf(prompt);
    hide();
    setState('');
    lockBusy();
    answering = true;
    try {
      const result = await post(`/api/panes/${encodeURIComponent(pane)}/prompt/choose`, {key});
      if (!result.ok) {
        answeredSignature = '';
        if (saved) render(saved, savedScreen);
        setState(result.data.detail || `HTTP ${result.status}`, true);
        return;
      }
      setState('');
      unlockBusy();                     // 先放开锁：跟进 poll 靠它判断
      setTimeout(poll, 1500);
    } catch (error) {
      answeredSignature = '';
      if (saved) render(saved, savedScreen);
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      answering = false;
      unlockBusy();
    }
  }

  function setState(text, bad) {
    const node = $('state');
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('bad', !!bad);
  }

  async function poll() {
    if (busy || document.hidden) return;
    const pane = paneNow();
    if (!pane) { if (prompt) hide(); return; }
    lockBusy();
    try {
      const response = await fetch(`/api/panes/${encodeURIComponent(pane)}/prompt`, {credentials: 'same-origin'});
      if (!response.ok) { if (prompt) hide(); return; }
      const data = await response.json();
      if (!data.prompt) { answeredSignature = ''; if (prompt) hide(); return; }
      if (hiddenByUser) return;
      // 有授权就按授权答：这是"记住这个选择"的全部意义，但也只在这道题的指纹上生效。
      if (data.grant && autoAnswered !== data.signature) {
        if (answering) return;            // 手里还有一条在发，下一轮再试，别互相踩
        const action = JSON.parse(data.grant.action || '{}');
        autoAnswered = data.signature;
        answeredSignature = signatureOf(data.prompt);
        const label = data.grant.label || '同一道题';
        const scopeText = data.grant.scope === 'always' ? '一直' : '本轮';
        try {
          if (Array.isArray(action.toggle)) await chooseToggle(action.toggle[0], action.toggle[1]);
          else if (Number.isInteger(action.index)) await chooseByIndex(action.index);
          else if (action.key) await chooseByKey(action.key);
          await fetch(`/api/panes/${encodeURIComponent(pane)}/approvals/${encodeURIComponent(data.grant.id)}/used`,
            {method: 'POST', credentials: 'same-origin'});
          // 说清楚"它替你点了什么"：自动答题必须留痕，不能悄悄发生。
          setState(`已按你的授权自动选了「${label}」（${scopeText}）`);
        } catch (_error) {
          setState(`自动应答没成功，请手动点一下`, true);
        }
        return;
      }
      if (!data.signature || data.signature !== autoAnswered) autoAnswered = '';
      // 答过的题：sweep 更新之前别再弹（点下那一刻就记了签名，卡片已经先收了）。
      const sig = signatureOf(data.prompt);
      if (sig === answeredSignature) return;
      answeredSignature = '';
      // 同一个菜单就别重绘（会打断用户正在按的方向键）。
      // 开关的"当前值"也要算进比较：只比标题/选项数/光标的话，←→ 切完之后卡片上的
      // 高亮永远停在旧值，人以为键盘没生效。
      const toggleKey = (found) => JSON.stringify((found && found.toggles || [])
        .map(item => [item.label, item.active]));
      if (prompt && prompt.title === data.prompt.title
          && prompt.options.length === data.prompt.options.length
          && prompt.selected === data.prompt.selected
          && toggleKey(prompt) === toggleKey(data.prompt)) return;
      render(data.prompt, data.screen, data.signature);
    } catch (_error) {
      // 网络抖动：保持现状，下一轮再说
    } finally {
      unlockBusy();
    }
  }

  function bindKeyboard() {
    const node = card();
    if (!node) return;
    node.addEventListener('keydown', event => {
      if (!prompt) return;
      const arrows = ['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight'];
      const hasOptions = !!(prompt.options && prompt.options.length);
      const toggles = prompt.toggles || [];
      if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && toggles.length && !hasOptions) {
        // 分段开关：卡片上写着 left/right to switch，按了就该动 —— 以前只处理上下键，
        // 于是键盘按下去毫无反应，只有鼠标点分段才好使。
        event.preventDefault();
        const toggle = toggles[0];
        const next = toggle.active + (event.key === 'ArrowRight' ? 1 : -1);
        if (next >= 0 && next < toggle.options.length) chooseToggle(toggle.index, next);
      } else if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && hasOptions) {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        cursor = (cursor + step + prompt.options.length) % prompt.options.length;
        paintCursor();
        const target = document.querySelectorAll('#' + CARD + '-options .prompt-option')[cursor];
        if (target) target.focus();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        // 只有一个开关（没有列表项）时，Enter 就是"确认当前值"，别去选第 0 项
        if (!prompt.options || !prompt.options.length) chooseByKey('enter');
        else chooseByIndex(cursor);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        chooseByKey('esc');
      }
    });
  }

  function start(paneGetter) {
    getPane = paneGetter;
    const node = card();
    if (!node) return;
    hiddenByUser = false;
    const close = $('close');
    if (close) close.onclick = () => { hiddenByUser = true; const box = card(); if (box) box.hidden = true; };
    const escButton = $('esc');
    if (escButton) escButton.onclick = () => chooseByKey('esc');
    const enterButton = $('enter');
    if (enterButton) enterButton.onclick = () => chooseByKey('enter');
    bindKeyboard();
    if (timer) clearInterval(timer);
    timer = setInterval(poll, POLL_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    poll();
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    hide();
  }

  function refresh() {
    hide();
    poll();
  }

  window.TmuxPrompt = {start, stop, refresh, poll};
})();
