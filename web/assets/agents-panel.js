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
  const expanded = new Set();
  let view;
  let loading = false;
  let loaded = false;

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
        {credentials: 'same-origin', signal: view.signal});
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
        {credentials: 'same-origin', signal: view.signal});
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

  }

  function setTab(next) {
    tab = next === 'events' ? 'events' : 'calls';
    if (view) view.abort();
    view = null;
    cache = [];
    events = [];
    expanded.clear();
    loaded = loading = false;
    const calls = $('agents-list'), evs = $('events-list');
    calls.replaceChildren();
    evs.replaceChildren();
    calls.hidden = tab !== 'calls';
    evs.hidden = tab !== 'events';
    $('agents-empty').hidden = $('events-empty').hidden = true;
    $('agents-count').textContent = '';
    const title = $('agents-title');
    if (title) title.textContent = tab === 'calls' ? '子 agent' : '操作日志';
    for (const [id, value] of [['ag-tab-calls', 'calls'], ['ag-tab-events', 'events']]) {
      const button = $(id);
      if (!button) continue;
      button.classList.toggle('on', value === tab);
      button.setAttribute('aria-selected', value === tab ? 'true' : 'false');
    }
    if (open && getPane()) {
      view = new AbortController();
      view.pane = getPane();
      refresh();
    }
  }

  let cache = [];
  function renderRows() {
    const list = $('agents-list');
    if (!list) return;
    list.innerHTML = '';
    for (const call of cache) list.appendChild(row(call));

  }

  async function refresh() {
    if (!open || !getPane()) return;
    if (!view || view.pane !== getPane()) { setTab(tab); return; }
    if (loading) return;
    const active = view;
    const calls = tab === 'calls';
    const pane = encodeURIComponent(active.pane);
    const empty = $(calls ? 'agents-empty' : 'events-empty');
    const count = $('agents-count');
    const url = calls ? `/api/agents/calls?pane=${pane}&limit=40` : `/api/events?pane=${pane}&limit=60`;
    loading = true;
    if (!loaded) {
      empty.textContent = '正在读取…';
      empty.hidden = false;
    }
    try {
      const response = await fetch(url, {credentials:'same-origin', signal:active.signal});
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      if (view !== active || getPane() !== active.pane) return;
      const items = calls ? data.calls : data.events;
      if (!Array.isArray(items)) throw new Error('invalid list');
      if (!loaded || JSON.stringify(items) !== JSON.stringify(calls ? cache : events)) {
        if (calls) {
          cache = items;
          renderRows();
        } else {
          events = items;
          renderEvents();
        }
      }
      loaded = true;
      const running = calls && items.filter(call => call.status === 'running').length;
      count.textContent = items.length ? items.length + ' 条' + (running ? ' · ' + running + ' 在跑' : '') : '';
      empty.textContent = calls ? '当前窗格还没有 relay 问答记录' : '当前窗格还没有操作记录';
      empty.hidden = items.length > 0;
    } catch (_error) {
      if (view === active) {
        if (loaded) count.textContent = '刷新失败';
        else empty.textContent = '暂时无法读取，稍后重试';
      }
    } finally {
      if (view === active) loading = false;
    }
  }

  function setOpen(next) {
    open = next;
    const panel = $('agents-panel');
    if (panel) panel.hidden = !open;
    const button = $('agents-button');
    if (button) button.setAttribute('aria-pressed', open ? 'true' : 'false');
    if (open) {
      setTab('calls');
      clearInterval(timer);
      timer = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
    } else {
      clearInterval(timer);
      timer = 0;
      if (view) view.abort();
      view = null;
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
    refresh,
    tab: value => setTab(value),
    state: () => ({open, tab, count: cache.length, events: events.length}),
  };
})();
