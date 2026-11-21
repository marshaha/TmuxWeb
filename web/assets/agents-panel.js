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

  // ---- 操作日志（第二个标签）
  let tab = 'calls';
  let events = [];
  const KIND_LABEL = {
    send: '发消息', key: '按键', exec: '跑命令', 'session.kill': '删会话',
    'session.create': '建会话', rename: '改名',
  };
  const STATUS_LABEL = {running: '在跑', ok: '成功', error: '失败', unknown: '结果不明', retrying: '等回车'};

  function eventRow(event) {
    const item = document.createElement('div');
    item.className = 'ag-call ' + (event.status === 'ok' ? 'ok' : event.status === 'running' ? 'running' : 'error');
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'ag-head';
    head.innerHTML = `<span class="ag-dot ${event.status === 'ok' ? 'ok' : event.status === 'running' ? 'running' : event.status === 'retrying' ? 'timeout' : 'error'}" aria-hidden="true"></span>`
      + `<span class="ag-who">${esc(event.actor || 'ui')}</span>`
      + `<span class="ag-meta"><span class="ag-kind">${esc(KIND_LABEL[event.kind] || event.kind)}</span> ${esc(STATUS_LABEL[event.status] || event.status)}`
      + `${event.duration ? ' · ' + seconds(event.duration) : ''}</span>`
      + `<span class="ag-q">${esc(event.summary || event.error || '')}</span>`;
    head.addEventListener('click', () => toggleEvent(event.id, item));
    item.appendChild(head);
    item.dataset.eventId = event.id;
    return item;
  }

  async function toggleEvent(eventId, item) {
    const existing = item.querySelector('.ag-body');
    if (existing) { existing.remove(); return; }
    try {
      const response = await fetch(`/api/events/${encodeURIComponent(eventId)}`,
        {credentials: 'same-origin'});
      if (!response.ok) return;
      const full = await response.json();
      const body = document.createElement('div');
      body.className = 'ag-body';
      body.innerHTML = `<div class="ag-label">请求</div><pre class="ag-text">${esc(JSON.stringify(full.request, null, 2))}</pre>`
        + `<div class="ag-label">结果</div><pre class="ag-text">${esc(full.error || JSON.stringify(full.result, null, 2) || '')}</pre>`;
      item.appendChild(body);
    } catch (_error) { /* 打不开就算了 */ }
  }

  function renderEvents() {
    const list = $('events-list');
    if (!list) return;
    list.innerHTML = '';
    for (const event of events) list.appendChild(eventRow(event));
    const empty = $('events-empty');
    if (empty) empty.hidden = events.length > 0;
  }

  async function loadEvents() {
    const pane = getPane();
    if (!open || !pane) return;
    try {
      const response = await fetch(`/api/events?pane=${encodeURIComponent(pane)}&limit=60`,
        {credentials: 'same-origin'});
      if (!response.ok) return;
      const data = await response.json();
      events = data.events || [];
      const count = $('agents-count');
      if (count) count.textContent = events.length ? `${events.length} 条` : '';
      renderEvents();
    } catch (_error) { /* 下一轮再说 */ }
  }

  function setTab(next) {
    tab = next;
    const calls = $('agents-list'), evs = $('events-list');
    const callsEmpty = $('agents-empty'), evsEmpty = $('events-empty');
    if (calls) calls.hidden = tab !== 'calls';
    if (evs) evs.hidden = tab !== 'events';
    if (callsEmpty) callsEmpty.hidden = tab !== 'calls' || cache.length > 0;
    if (evsEmpty) evsEmpty.hidden = tab !== 'events' || events.length > 0;
    const title = $('agents-title');
    if (title) title.textContent = tab === 'calls' ? '子 agent' : '操作日志';
    for (const [id, value] of [['ag-tab-calls', 'calls'], ['ag-tab-events', 'events']]) {
      const button = $(id);
      if (!button) continue;
      button.classList.toggle('on', value === tab);
      button.setAttribute('aria-selected', value === tab ? 'true' : 'false');
    }
    if (tab === 'calls') load(); else loadEvents();
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
      setTab(tab);
      clearInterval(timer);
      timer = setInterval(() => { if (!document.hidden) (tab === 'calls' ? load() : loadEvents()); }, POLL_MS);
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
    const tabCalls = $('ag-tab-calls');
    if (tabCalls && !tabCalls.dataset.bound) {
      tabCalls.dataset.bound = '1';
      tabCalls.addEventListener('click', () => setTab('calls'));
    }
    const tabEvents = $('ag-tab-events');
    if (tabEvents && !tabEvents.dataset.bound) {
      tabEvents.dataset.bound = '1';
      tabEvents.addEventListener('click', () => setTab('events'));
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
    refresh: () => (tab === 'calls' ? load() : loadEvents()),
    tab: value => setTab(value),
    state: () => ({open, tab, count: cache.length, events: events.length}),
  };
})();
