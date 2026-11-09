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
  let prompt = null;
  let cursor = 0;
  let hiddenByUser = false;      // 用户点了 ✕：这一轮别再弹

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
    if (!list) return;
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
    if (!pane || busy) return;
    busy = true;
    try {
      const result = await post(`/api/panes/${encodeURIComponent(pane)}/prompt/choose`, {index});
      if (!result.ok) { setState(result.data.detail || `HTTP ${result.status}`, true); return; }
      if (result.data.prompt) {
        render(result.data.prompt, (result.data.prompt.raw || '').split('\n'));
      } else {
        hide();
      }
    } catch (error) {
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      busy = false;
    }
  }

  async function chooseToggle(toggleIndex, value) {
    const pane = paneNow();
    if (!pane || busy) return;
    busy = true;
    try {
      const result = await post(`/api/panes/${encodeURIComponent(pane)}/prompt/choose`,
        {toggle: toggleIndex, value});
      if (!result.ok) { setState(result.data.detail || `HTTP ${result.status}`, true); return; }
      if (result.data.prompt) render(result.data.prompt, (result.data.prompt.raw || '').split('\n'));
      else hide();
    } catch (error) {
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      busy = false;
    }
  }

  async function chooseByKey(key) {
    const pane = paneNow();
    if (!pane || busy) return;
    busy = true;
    try {
      const result = await post(`/api/panes/${encodeURIComponent(pane)}/prompt/choose`, {key});
      if (!result.ok) { setState(result.data.detail || `HTTP ${result.status}`, true); return; }
      if (result.data.prompt) render(result.data.prompt, (result.data.prompt.raw || '').split('\n'));
      else hide();
    } catch (error) {
      setState('没送到，再试一次：' + error.message, true);
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

  async function poll() {
    if (busy || document.hidden) return;
    const pane = paneNow();
    if (!pane) { if (prompt) hide(); return; }
    busy = true;
    try {
      const response = await fetch(`/api/panes/${encodeURIComponent(pane)}/prompt`, {credentials: 'same-origin'});
      if (!response.ok) { if (prompt) hide(); return; }
      const data = await response.json();
      if (!data.prompt) { if (prompt) hide(); return; }
      if (hiddenByUser) return;
      // 同一个菜单就别重绘（会打断用户正在按的方向键）
      if (prompt && prompt.title === data.prompt.title
          && prompt.options.length === data.prompt.options.length
          && prompt.selected === data.prompt.selected) return;
      render(data.prompt, data.screen);
    } catch (_error) {
      // 网络抖动：保持现状，下一轮再说
    } finally {
      busy = false;
    }
  }

  function bindKeyboard() {
    const node = card();
    if (!node) return;
    node.addEventListener('keydown', event => {
      if (!prompt) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        cursor = (cursor + step + prompt.options.length) % prompt.options.length;
        paintCursor();
        document.querySelectorAll('#' + CARD + '-options .prompt-option')[cursor].focus();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        chooseByIndex(cursor);
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
