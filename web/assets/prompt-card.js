/* 聊天里的「交互菜单」卡片：把 pane 里正在等的选择搬进聊天，不用切终端。
 *
 * 场景：kimi 的 /model 选择器、claude 的「Do you want to proceed?」、kimi 的
 * 「1. Compact and continue」—— 这些是 TUI 占满屏幕画的，聊天里原本只看到
 * "已发送"。服务端从抓屏里解析出来（agent_prompts.py），这里渲染成按钮。
 *
 * 原则：
 *   - 永远不自动应答；只有人点了才发键；
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
  // 乐观交互：点下去先在卡片上生效，再等服务器/pane 的真实状态来对齐。
  // 否则每次点击都要等一个往返 + 一次轮询，远端面板上就是"卡一下"。
  let optimistic = null;         // {toggle, value, at} 或 {index, at}
  let lastScreen = [];
  const OPTIMISTIC_TTL = 3000;

  const $ = id => document.getElementById(CARD + '-' + id);

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  }

  function paneNow() {
    try { return getPane ? getPane() : ''; } catch (_error) { return ''; }
  }

  function card() { return document.getElementById(CARD); }

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

  function render(data, screen) {
    const node = card();
    if (!node) return;
    prompt = data;
    cursor = Number.isInteger(data.selected) ? Math.min(data.selected, data.options.length - 1) : 0;

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

  function paintOptimistic() {
    if (!optimistic || !prompt) return;
    if (optimistic.toggle != null) {
      const rows = document.querySelectorAll('#' + CARD + '-toggles .prompt-toggle');
      const group = rows[optimistic.toggle];
      if (group) {
        [...group.querySelectorAll('.prompt-toggle-btn')].forEach((button, index) => {
          const on = index === optimistic.value;
          button.classList.toggle('on', on);
          button.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
      }
    }
    if (optimistic.index != null) {
      const rows = document.querySelectorAll('#' + CARD + '-options .prompt-option');
      rows.forEach((row, index) => {
        row.classList.toggle('picked', index === optimistic.index);
        if (index === optimistic.index && !row.dataset.sent) {
          row.dataset.sent = '1';
          row.dataset.label = row.innerHTML;
          // 用选项自己的文案（row.textContent 会把右边的 meta 也拼进来，变成「继续Enter」）
          const label = (prompt.options && prompt.options[index] && prompt.options[index].label)
            || row.textContent.trim();
          row.innerHTML = esc(label) + ' · 已发送';
          row.disabled = true;
        }
      });
    }
  }

  function settleOptimistic(found) {
    if (!optimistic) return;
    const agrees = found && (
      (optimistic.toggle != null && (found.toggles || [])
        .some(item => item.index === optimistic.toggle && item.active === optimistic.value))
      || (optimistic.index != null && found.selected === optimistic.index));
    if (agrees) {
      optimistic = null;
      setState('');                    // 屏幕已经对上了，别把"等刷新…"留在那儿
      return;
    }
    if (Date.now() - optimistic.at < OPTIMISTIC_TTL) return;   // 还在路上
    // 过了窗口还没对齐：把卡片退回服务器的真实状态，并说清楚
    optimistic = null;
    render(found || prompt, lastScreen);
    setState('屏幕没有变化，再点一次或切到终端', true);
  }

  async function post(url, body) {
    const response = await fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    return {ok: response.ok, status: response.status, data};
  }

  async function followUp() {
    for (const delay of [120, 300, 700, 1400]) {
      await new Promise(resolve => setTimeout(resolve, delay));
      const before = prompt && prompt.title;
      await poll();
      if (!prompt) {                      // 菜单没了：回执照旧留着，1.2 秒后收起
        const box = card();
        if (box) box.hidden = false;
        setTimeout(() => { if (!prompt) hide(); }, 1200);
        return;
      }
      if (!before || prompt.title !== before) return;   // 换题了，交给卡片自己显示
    }
  }

  function optionLabel(index) {
    if (!prompt || !prompt.options || !prompt.options[index]) return String(index + 1);
    return prompt.options[index].label || String(index + 1);
  }

  async function chooseByIndex(index) {
    const pane = paneNow();
    if (!pane) return;
    if (busy) { setState('上一条还在发送，稍等一下…'); return; }   // 别静默吞掉点击
    optimistic = {index, at: Date.now()};
    cursor = index;
    paintCursor();
    paintOptimistic();
    setState('已发送：' + optionLabel(index) + '（等屏幕刷新…）', false);
    lockBusy();
    try {
      const result = await post(`/api/panes/${encodeURIComponent(pane)}/prompt/choose`, {index});
      if (!result.ok) {
        optimistic = null;
        render(prompt, lastScreen);
        setState(result.data.detail || `HTTP ${result.status}`, true);
        return;
      }
      unlockBusy();                     // 先放开锁：followUp 里的 poll 靠它判断
      await followUp();
    } catch (error) {
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      unlockBusy();
    }
  }

  async function chooseToggle(toggleIndex, value) {
    const pane = paneNow();
    if (!pane) return;
    if (busy) { setState('上一条还在发送，稍等一下…'); return; }
    if (prompt && prompt.toggles && prompt.toggles[toggleIndex]
        && prompt.toggles[toggleIndex].active === value) {
      setState('已经是这个值了');
      return;
    }
    // 先让卡片跟着手走（下一帧生效），请求在后台跑
    optimistic = {toggle: toggleIndex, value, at: Date.now()};
    paintOptimistic();
    setState('已发送（等屏幕刷新…）', false);
    lockBusy();
    try {
      const result = await post(`/api/panes/${encodeURIComponent(pane)}/prompt/choose`,
        {toggle: toggleIndex, value});
      if (!result.ok) {
        optimistic = null;
        render(prompt, lastScreen);
        setState(result.data.detail || `HTTP ${result.status}`, true);
        return;
      }
      if (result.data.unchanged) {
        optimistic = null;
        render(prompt, lastScreen);
        setState('已经是这个值了');
        return;
      }
      setState('已发送（等屏幕刷新…）', false);
      unlockBusy();                     // 先放开锁：followUp 里的 poll 靠它判断
      await followUp();
    } catch (error) {
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      unlockBusy();
    }
  }

  async function chooseByKey(key) {
    const pane = paneNow();
    if (!pane) return;
    if (busy) { setState('上一条还在发送，稍等一下…'); return; }
    lockBusy();
    try {
      const result = await post(`/api/panes/${encodeURIComponent(pane)}/prompt/choose`, {key});
      if (!result.ok) { setState(result.data.detail || `HTTP ${result.status}`, true); return; }
      setState('已发送', false);
      unlockBusy();                     // 先放开锁：followUp 里的 poll 靠它判断
      await followUp();
    } catch (error) {
      setState('没送到，再试一次：' + error.message, true);
    } finally {
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
      if (!data.prompt) { if (prompt) hide(); return; }
      if (hiddenByUser) return;
      // 同一个菜单就别重绘（会打断用户正在按的方向键）。
      // 开关的"当前值"也要算进比较：只比标题/选项数/光标的话，←→ 切完之后卡片上的
      // 高亮永远停在旧值，人以为键盘没生效。
      settleOptimistic(data.prompt);
      const toggleKey = (found) => JSON.stringify((found && found.toggles || [])
        .map(item => [item.label, item.active]));
      if (prompt && prompt.title === data.prompt.title
          && prompt.options.length === data.prompt.options.length
          && prompt.selected === data.prompt.selected
          && toggleKey(prompt) === toggleKey(data.prompt)) return;
      render(data.prompt, data.screen);
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
