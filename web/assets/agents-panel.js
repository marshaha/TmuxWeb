/* 子 agent 调用图：谁问过谁、问的是什么、答完没有。
 *
 * 数据来自服务端 /api/agents/calls（每次 relay ask 都记一条父→子的边）。
 * 只在面板打开时轮询：关着就不发请求，和 prompt-card 的做法一致。
 *
 *   TmuxAgents.mount({getPane, toast})
 */
(function () {
  const POLL_MS = 4000;
  let getPane = () => '';
  let timer = 0;
  let open = false;
  let expanded = new Set();

  const $ = id => document.getElementById(id);

  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, ch => (
      {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  }

  function seconds(value) {
    const n = Number(value || 0);
    if (!n) return '';
    return n < 10 ? n.toFixed(1) + 's' : Math.round(n) + 's';
  }

  function statusText(call) {
    if (call.status === 'running') return '在等回复';
    if (call.status === 'timeout') return '超时';
    if (call.status === 'error') return '失败';
    return '已回复';
  }

  function row(call) {
    const item = document.createElement('div');
    item.className = 'ag-call ' + call.status;
    item.dataset.callId = call.id;
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'ag-head';
    head.innerHTML = `<span class="ag-dot ${call.status}" aria-hidden="true"></span>`
      + `<span class="ag-who">${esc(call.child || '(未知)')}</span>`
      + `<span class="ag-meta">${esc(statusText(call))}${call.duration ? ' · ' + seconds(call.duration) : ''}</span>`
      + `<span class="ag-q">${esc((call.question || '').split('\n')[0].slice(0, 120))}</span>`;
    head.addEventListener('click', () => toggle(call.id));
    item.appendChild(head);
    if (expanded.has(call.id)) {
      const body = document.createElement('div');
      body.className = 'ag-body';
      body.innerHTML = `<div class="ag-label">问</div><pre class="ag-text">${esc(call.question || '')}</pre>`
        + `<div class="ag-label">答</div><pre class="ag-text">${esc(call.reply || call.reply_preview || (call.error ? '（没有回复：' + call.error + '）' : '（还在等）'))}</pre>`;
      item.appendChild(body);
    }
    return item;
  }

  async function toggle(callId) {
    if (expanded.has(callId)) {
      expanded.delete(callId);
      renderRows();
      return;
    }
    expanded.add(callId);
    renderRows();
    try {
      const response = await fetch(`/api/agents/calls/${encodeURIComponent(callId)}`,
        {credentials: 'same-origin'});
      if (!response.ok) return;
      const full = await response.json();
      const item = document.querySelector(`.ag-call[data-call-id="${CSS.escape(callId)}"]`);
      if (!item) return;
      const body = item.querySelector('.ag-body');
      if (body) {
        body.innerHTML = `<div class="ag-label">问</div><pre class="ag-text">${esc(full.question || '')}</pre>`
          + `<div class="ag-label">答</div><pre class="ag-text">${esc(full.reply || (full.error ? '（没有回复：' + full.error + '）' : '（还在等）'))}</pre>`;
      }
    } catch (_error) { /* 展开失败就保持预览 */ }
  }

  let cache = [];
  function renderRows() {
    const list = $('agents-list');
    if (!list) return;
    list.innerHTML = '';
    for (const call of cache) list.appendChild(row(call));
    const empty = $('agents-empty');
    if (empty) empty.hidden = cache.length > 0;
  }

  async function load() {
    const pane = getPane();
    if (!open || !pane) return;
    try {
      const response = await fetch(`/api/agents/calls?pane=${encodeURIComponent(pane)}&limit=40`,
        {credentials: 'same-origin'});
      if (!response.ok) return;
      const data = await response.json();
      cache = data.calls || [];
      const count = $('agents-count');
      if (count) {
        const running = cache.filter(call => call.status === 'running').length;
        count.textContent = cache.length ? (running ? `${cache.length} 个 · ${running} 在跑` : `${cache.length} 个`) : '';
      }
      renderRows();
    } catch (_error) { /* 网络抖动：下一轮再说 */ }
  }

  function setOpen(next) {
    open = next;
    const panel = $('agents-panel');
    if (panel) panel.hidden = !open;
    const button = $('agents-button');
    if (button) button.setAttribute('aria-pressed', open ? 'true' : 'false');
    if (open) {
      load();
      clearInterval(timer);
      timer = setInterval(() => { if (!document.hidden) load(); }, POLL_MS);
    } else {
      clearInterval(timer);
      timer = 0;
    }
  }

  function bind() {
    const button = $('agents-button');
    if (button && !button.dataset.bound) {
      button.dataset.bound = '1';
      button.addEventListener('click', () => setOpen(!open));
    }
    const close = $('agents-close');
    if (close && !close.dataset.bound) {
      close.dataset.bound = '1';
      close.addEventListener('click', () => setOpen(false));
    }
  }

  window.TmuxAgents = {
    mount(options) {
      if (options && options.getPane) getPane = options.getPane;
      bind();
    },
    open: () => setOpen(true),
    close: () => setOpen(false),
    refresh: load,
    state: () => ({open, count: cache.length}),
  };
})();
