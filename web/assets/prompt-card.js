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
  let notifyAutoAnswer = null;
  let timer = null;
  let busy = false;
  let pollController = null;
  let paneEpoch = 0;
  let controlsBound = false;
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
  let lastScreen = [];

  const $ = id => document.getElementById(CARD + '-' + id);

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
      found.selected, found.revision,
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
      const selected = prompt.kind === 'multi' ? prompt.options[index].checked : index === cursor;
      node.setAttribute('aria-selected', String(!!selected));
    });
  }

  function render(data, screen, signature) {
    const node = card();
    if (!node) return;
    prompt = data;
    cursor = Math.max(0, Math.min(data.selected || 0, data.options.length - 1));

    if (signature !== undefined) lastSignature = signature || '';
    $('title').textContent = data.title || (data.kind === 'confirm' ? '需要确认' : '等待选择');
    const hint = $('hint');
    hint.textContent = data.read_only ? '屏幕预览，请在终端操作。' : data.hint || (data.more ? `屏幕外还有 ${data.more} 项` : '');
    hint.hidden = !hint.textContent;

    const list = $('options');
    list.innerHTML = '';
    list.setAttribute('aria-multiselectable', String(data.kind === 'multi'));
    data.options.forEach((option, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'prompt-option';
      button.setAttribute('role', 'option');
      button.dataset.index = String(index);
      button.disabled = !!data.read_only;
      const label = document.createElement('span');
      label.className = 'prompt-label';
      label.textContent = (option.number ? option.number + '. ' : '') + (option.label || '');
      const text = document.createElement('span');
      text.className = 'prompt-option-text';
      text.appendChild(label);
      if (option.description) {
        const description = document.createElement('span');
        description.className = 'prompt-desc';
        description.textContent = option.description;
        text.appendChild(description);
      }
      button.appendChild(text);
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
        button.disabled = !!data.read_only;
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
    if (!data.read_only && data.kind !== 'multi' && data.kind !== 'screen') {
      const remember = rememberRow(data);
      remember.id = 'remember-row';
      const anchor = toggles.hidden ? list : toggles;
      if (anchor.after) anchor.after(remember);
      else if (anchor.parentNode) anchor.parentNode.appendChild(remember);
    }

    lastScreen = screen || [];
    // 手机上没有回车键。开关类界面（kimi 的 Thinking：←→ 选、Enter 确认）和提示里
    // 明写 confirm/继续/continue 的，给一颗「确认 Enter」按钮，点它等于敲回车。
    const enterButton = $('enter');
    if (enterButton) {
      const hint = String(data.hint || '');
      const needsEnter = data.kind === 'multi' || data.kind === 'toggle'
        || /press enter/i.test(hint)
        || (/confirm|continue|确认|继续/i.test(hint) && !(data.options || []).length);
      enterButton.hidden = !!data.read_only || !needsEnter;
    }
    const dismiss = $('esc');
    if (dismiss) dismiss.hidden = !!data.read_only || data.kind === 'screen';
    const raw = $('raw');
    if (raw) raw.textContent = (screen || []).join('\n');
    const details = $('raw-wrap');
    if (details) { details.hidden = !screen || !screen.length; details.open = data.kind === "screen"; }

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

  function chooseByIndex(index) {
    const option = prompt && prompt.options[index];
    return submit({index}, option && option.label);
  }

  function chooseToggle(toggleIndex, value) {
    if (prompt && prompt.toggles[toggleIndex]?.active === value) {
      setState('已经是这个值了');
      return;
    }
    return submit({toggle: toggleIndex, value}, `开关 ${toggleIndex} = ${value}`);
  }

  function chooseByKey(key) {
    return submit({key});
  }

  async function submit(action, label, saved = prompt, automatic = false) {
    const pane = paneNow();
    const epoch = paneEpoch;
    if (busy) { setState('上一条还在发送，稍等一下…'); return false; }
    if (!pane || !saved || saved.read_only || saved.kind === 'screen') return false;
    const screen = lastScreen;
    const checklistChange = saved.kind === "multi" && action.index !== undefined;
    pollController?.abort?.();
    pollController = null;
    const remember = !automatic && label && saved.kind !== 'multi' && rememberChoice && lastSignature
      ? {signature: lastSignature, action: action.toggle === undefined
        ? {...action, revision: saved.revision}
        : {toggle: [action.toggle, action.value], revision: saved.revision},
        scope: rememberScope, turn: turnKey(), label}
      : null;
    const active = () => pane === paneNow() && epoch === paneEpoch;
    answeredSignature = signatureOf(saved);
    if (!checklistChange) hide();
    setState(checklistChange ? '正在更新选项…' : '');
    busy = true;
    try {
      const result = await post(`/api/panes/${encodeURIComponent(pane)}/prompt/choose`,
        {...action, revision: saved.revision});
      if (!active()) return !!result.ok;
      if (!result.ok || result.data.unchanged) {
        answeredSignature = '';
        render(saved, screen);
        setState(result.data.detail || (result.ok ? '已经是这个值了' : `HTTP ${result.status}`), !result.ok);
        return false;
      }
      if (remember) void postJson(`/api/panes/${encodeURIComponent(pane)}/approvals`, remember).catch(() => {});
      setTimeout(() => { if (active()) poll(false, manual); }, checklistChange ? 350 : 1500);
      return true;
    } catch (error) {
      if (active()) {
        answeredSignature = '';
        render(saved, screen);
        setState('没送到，再试一次：' + error.message, true);
      }
      return false;
    } finally {
      busy = false;
    }
  }

  function setState(text, bad) {
    const node = $('state');
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('bad', !!bad);
  }

  let manual = false;
  async function poll(explicit = false, inspect = manual) {
    if ((!timer && !inspect) || (busy && !explicit) || document.hidden || pollController) return;
    const pane = paneNow();
    if (!pane) { if (prompt) hide(); return; }
    const epoch = paneEpoch;
    const controller = typeof AbortController === 'undefined' ? {} : new AbortController();
    pollController = controller;
    try {
      const turn = turnKey();
      const query = inspect ? '?inspect=true' : (turn ? '?turn=' + encodeURIComponent(turn) : '');
      const response = await fetch(`/api/panes/${encodeURIComponent(pane)}/prompt${query}`,
        {credentials: 'same-origin', signal: controller.signal});
      if (pollController !== controller || pane !== paneNow() || epoch !== paneEpoch) return;
      const data = await response.json();
      if (pollController !== controller || pane !== paneNow() || epoch !== paneEpoch || busy) return;
      if (!response.ok) {
        if (explicit) {
          render({kind: 'screen', title: '读取交互失败', options: []}, []);
          setState(data.detail || `HTTP ${response.status}`, true);
        } else if (prompt) hide();
        return;
      }
      if (!data.prompt) {
        answeredSignature = '';
        if (explicit) render({kind: 'screen', title: '未识别到可操作的选项',
          hint: '可核对屏幕原文，或用终端继续操作。', options: []}, data.screen);
        else if (prompt && prompt.kind !== 'screen') hide();
        return;
      }
      if (hiddenByUser && !explicit) return;
      const gated = document.getElementById('needs-you-card');
      if (gated && !gated.hidden && !explicit) return;
      const grantAction = data.grant && !inspect ? JSON.parse(data.grant.action || '{}') : null;
      if (grantAction?.revision && grantAction.revision === data.prompt.revision
          && autoAnswered !== data.signature) {
        const action = grantAction;
        const payload = Array.isArray(action.toggle) ? {toggle: action.toggle[0], value: action.toggle[1]} : action;
        autoAnswered = data.signature;
        lastScreen = data.screen || [];
        if (await submit(payload, data.grant.label, data.prompt, true)) {
          notifyAutoAnswer?.(`已按你的授权自动选了「${data.grant.label || '已保存选项'}」`);
          await fetch(`/api/panes/${encodeURIComponent(pane)}/approvals/${encodeURIComponent(data.grant.id)}/used`,
            {method: 'POST', credentials: 'same-origin'});
        }
        return;
      }
      if (autoAnswered !== data.signature) autoAnswered = '';
      const sig = signatureOf(data.prompt);
      if (!explicit && (sig === answeredSignature || sig === signatureOf(prompt))) {
        if (manual && prompt?.kind === 'multi' && sig === answeredSignature) {
          setState('等待屏幕更新，可重新查看交互。');
        }
        return;
      }
      answeredSignature = '';
      setState('');
      render({...data.prompt, grant: data.grant}, data.screen, data.signature);
    } catch (error) {
      if (explicit && pollController === controller && epoch === paneEpoch && pane === paneNow()) {
        render({kind: 'screen', title: '读取交互失败', options: []}, []);
        setState(error.message, true);
      }
    } finally {
      if (pollController === controller) pollController = null;
    }
  }

  function bindKeyboard() {
    const node = card();
    if (!node) return;
    node.addEventListener('keydown', event => {
      if (!prompt || prompt.read_only || prompt.kind === 'screen') return;
      if (event.target?.closest?.('input, select, summary, .prompt-foot, .prompt-head')) return;
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
      } else if (prompt.kind === 'multi' && event.key === ' ' && hasOptions) {
        event.preventDefault();
        chooseByIndex(cursor);
      } else if (event.key === 'Enter') {
        event.preventDefault();
        // 只有一个开关（没有列表项）时，Enter 就是"确认当前值"，别去选第 0 项
        if (prompt.kind === 'multi' || !prompt.options.length) chooseByKey('enter');
        else chooseByIndex(cursor);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        chooseByKey('esc');
      }
    });
  }

  function bindControls() {
    if (controlsBound || !card()) return;
    controlsBound = true;
    const close = $('close');
    if (close) close.onclick = () => {
      manual = false;
      hiddenByUser = true;
      card().hidden = true;
    };
    const escButton = $('esc');
    if (escButton) escButton.onclick = () => chooseByKey('esc');
    const enterButton = $('enter');
    if (enterButton) enterButton.onclick = () => chooseByKey('enter');
    const terminal = $('terminal');
    if (terminal) terminal.onclick = () => document.getElementById('terminal-mode-button')?.click();
    bindKeyboard();
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  }

  function start(paneGetter, onAutoAnswer) {
    getPane = paneGetter;
    notifyAutoAnswer = onAutoAnswer;
    bindControls();
    if (!timer) {
      timer = setInterval(poll, POLL_MS);
      poll();
    }
  }

  function reset() {
    paneEpoch++;
    pollController?.abort?.();
    pollController = null;
    manual = false;
    answeredSignature = '';
    lastSignature = '';
    autoAnswered = '';
    rememberChoice = false;
    hide();
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    reset();
  }

  function refresh() {
    reset();
    poll();
  }

  function inspect(paneGetter) {
    getPane = paneGetter;
    bindControls();
    pollController?.abort?.();
    pollController = null;
    manual = true;
    return poll(true, true);
  }

  window.TmuxPrompt = {start, stop, refresh, poll, inspect, isInspecting: () => manual};

})();
