/* The session sidebar, shared by the board (/home) and the multi-window page (/grid).
 *
 *   TmuxShell.mount({ onPane, onHosts })
 *
 * Renders the host → session → window → pane tree into #tree, keeps the busy/idle dots
 * fresh, lets a row be starred (favourites feed the board), and owns the collapse /
 * resize behaviour. Selection of panes is the page's business: it gets onPane(paneId).
 */
(() => {
  const state = {
    hosts: [], states: new Map(), favorites: new Set(), open: new Set(), collapsed: false, hooks: {},
  };

  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));

  async function api(url, options = {}) {
    const response = await fetch(url, { credentials: 'same-origin', headers: {'Accept': 'application/json'}, ...options });
    if (response.status === 401) { location = '/login'; throw new Error('登录失效'); }
    if (!response.ok) {
      let detail = {};
      try { detail = await response.json(); } catch {}
      throw new Error((detail && detail.detail) || `HTTP ${response.status}`);
    }
    return response.json();
  }

  function treeDetails(key, label, children, count, pad, tail) {
    const open = state.open.has(key) ? ' open' : '';
    return `<details data-tree="${esc(key)}"${open}><summary><span class="caret">▶</span>`
      + `<span class="tree-label" style="padding-left:${pad}px">${esc(label)}</span>`
      + `<span class="tree-count">${count}</span>${tail || ''}</summary>`
      + `<div class="tree-children">${children}</div></details>`;
  }

  function paneRow(pane) {
    const status = state.states.get(pane.id) || 'unknown';
    const starred = state.favorites.has(pane.id);
    const name = pane.title || `pane ${pane.index ?? ''} · ${pane.command || ''}`;
    return `<button class="tpane" data-pane="${esc(pane.id)}" title="${esc(pane.path || '')}">`
      + `<span class="trow"><span class="tstate ${esc(status)}"></span><span class="tname">${esc(name)}</span>`
      + `<span class="tstar ${starred ? 'on' : ''}" data-star="${esc(pane.id)}" role="button" tabindex="-1"`
      + ` title="${starred ? '取消收藏' : '收藏到主页'}">`
      + `<svg class="ico"><use href="#i-${starred ? 'star-fill' : 'star'}"/></svg></span></span>`
      + `<span class="tmeta">${esc([pane.command, pane.session].filter(Boolean).join(' · '))}</span></button>`;
  }

  function renderTree() {
    const tree = $('tree');
    if (!tree) return;
    if (!state.hosts.length) {
      tree.innerHTML = '<div class="empty" style="margin:10px;padding:14px;font-size:12px">还没有会话</div>';
      return;
    }
    tree.innerHTML = state.hosts.map(host => {
      const sessions = new Map();
      for (const pane of host.panes || []) {
        const skey = pane.session || 'default';
        if (!sessions.has(skey)) sessions.set(skey, new Map());
        const windows = sessions.get(skey), wkey = `${pane.window}:${pane.window_name || ''}`;
        if (!windows.has(wkey)) windows.set(wkey, []);
        windows.get(wkey).push(pane);
      }
      const sessionHtml = [...sessions].map(([session, windows]) => {
        const windowHtml = [...windows].map(([wkey, panes]) => {
          const first = panes[0];
          const label = `${first.window ?? ''}${first.window_name ? ': ' + first.window_name : ''}`;
          return treeDetails(`${host.id}/${session}/${wkey}`, label, panes.map(paneRow).join(''), panes.length, 8);
        }).join('');
        return treeDetails(`${host.id}/${session}`, session, windowHtml, `${windows.size}`, 4);
      }).join('');
      const tail = `<button class="trefresh" type="button" data-refresh-host="${esc(host.id)}"`
        + ` title="刷新这台主机" aria-label="刷新主机"><svg class="ico"><use href="#i-refresh"/></svg></button>`;
      return treeDetails(host.id, host.name || host.id, sessionHtml, (host.panes || []).length, 0, tail);
    }).join('');
    const panes = state.hosts.reduce((total, host) => total + (host.panes || []).length, 0);
    const counter = $('side-count');
    if (counter) {
      counter.textContent = `${panes} panes`;
      counter.title = `${state.hosts.length} 台主机 · ${panes} 个 pane`;
    }
  }

  function saveOpen() {
    try { localStorage.setItem('tmux-tree-open', JSON.stringify([...state.open])); } catch {}
  }

  function loadOpen() {
    try {
      const saved = JSON.parse(localStorage.getItem('tmux-tree-open') || '[]');
      if (Array.isArray(saved)) state.open = new Set(saved);
    } catch {}
  }

  function treeKeys() {
    const keys = [];
    for (const host of state.hosts) {
      keys.push(host.id);
      for (const pane of host.panes || []) {
        keys.push(`${host.id}/${pane.session}`);
        keys.push(`${host.id}/${pane.session}/${pane.window}:${pane.window_name || ''}`);
      }
    }
    return keys;
  }

  async function loadHosts() {
    try {
      const data = await api('/api/hosts');
      state.hosts = data.hosts || [];
      renderTree();
      if (state.hooks.onHosts) state.hooks.onHosts(state.hosts);
      refreshStates().catch(() => {});
      loadFavorites().catch(() => {});
    } catch (error) {
      const tree = $('tree');
      if (tree) tree.innerHTML = `<div class="empty" style="margin:10px;padding:14px;font-size:12px">${esc(error.message)}</div>`;
    }
  }

  async function refreshStates() {
    try {
      const data = await api('/api/panes/status');
      state.states = new Map((data.panes || []).map(item => [item.id, item.status]));
      for (const button of document.querySelectorAll('#tree [data-pane]')) {
        const dot = button.querySelector('.tstate');
        if (dot) dot.className = 'tstate ' + (state.states.get(button.dataset.pane) || 'unknown');
      }
    } catch {}
  }

  async function loadFavorites() {
    try {
      const data = await api('/api/home/favorites');
      state.favorites = new Set((data.favorites || []).map(item => item.pane));
      for (const star of document.querySelectorAll('#tree [data-star]')) {
        const on = state.favorites.has(star.dataset.star);
        star.classList.toggle('on', on);
        star.title = on ? '取消收藏' : '收藏到主页';
        const use = star.querySelector('use');
        if (use) use.setAttribute('href', on ? '#i-star-fill' : '#i-star');
      }
    } catch {}
  }

  async function toggleStar(paneId) {
    const on = state.favorites.has(paneId);
    try {
      if (on) {
        await api(`/api/home/favorites/${encodeURIComponent(paneId)}`, {method: 'DELETE'});
        state.favorites.delete(paneId);
      } else {
        await api('/api/home/favorites', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pane: paneId}),
        });
        state.favorites.add(paneId);
      }
      await loadFavorites();
    } catch (error) {
      toast(error.message);
    }
  }

  function toast(text) {
    const el = document.querySelector('.shell-toast');
    if (!el) { alert(text); return; }
    el.textContent = text;
    el.classList.add('show');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.classList.remove('show'), 1800);
  }

  const narrow = () => matchMedia('(max-width:860px)').matches;

  // wide screens hide the column, narrow ones slide the drawer in
  function syncCollapsed(collapsed) {
    state.collapsed = collapsed;
    const shell = document.getElementById('shell');
    if (shell) {
      if (narrow()) {
        shell.classList.toggle('mobile-open', !collapsed);
        shell.classList.remove('collapsed');
      } else {
        shell.classList.toggle('collapsed', collapsed);
        shell.classList.remove('mobile-open');
      }
    }
    try { localStorage.setItem('tmux-sidebar-collapsed', collapsed ? '1' : '0'); } catch {}
  }

  function bind() {
    const tree = $('tree');
    tree?.addEventListener('toggle', event => {
      const details = event.target.closest('details[data-tree]');
      if (!details) return;
      if (details.open) state.open.add(details.dataset.tree);
      else state.open.delete(details.dataset.tree);
      saveOpen();
    }, true);

    tree?.addEventListener('click', event => {
      const star = event.target.closest('[data-star]');
      if (star) { event.preventDefault(); event.stopPropagation(); toggleStar(star.dataset.star); return; }
      const refresh = event.target.closest('[data-refresh-host]');
      if (refresh) { event.preventDefault(); event.stopPropagation(); refreshHost(refresh.dataset.refreshHost, refresh); return; }
      const row = event.target.closest('[data-pane]');
      if (row && state.hooks.onPane) state.hooks.onPane(row.dataset.pane, row);
    });

    $('tree-expand')?.addEventListener('click', () => { state.open = new Set(treeKeys()); saveOpen(); renderTree(); });
    $('tree-collapse')?.addEventListener('click', () => { state.open = new Set(); saveOpen(); renderTree(); });
    $('reload')?.addEventListener('click', () => { loadHosts(); toast('已刷新'); });
    $('side-collapse')?.addEventListener('click', () => syncCollapsed(true));
    $('side-open')?.addEventListener('click', () => syncCollapsed(false));
    $('drawer-backdrop')?.addEventListener('click', () => syncCollapsed(true));

    // drag the edge to resize, double-click to reset — same as the console
    const handle = $('side-resizer'), sidebar = $('sidebar');
    if (handle && sidebar) {
      const MIN = 200, DEFAULT = 268;
      const clamp = width => Math.max(MIN, Math.min(Math.min(640, Math.round(window.innerWidth * 0.6)), Math.round(width)));
      const apply = (width, save) => {
        const value = clamp(width);
        document.documentElement.style.setProperty('--side-w', value + 'px');
        if (save) { try { localStorage.setItem('tmux-side-w', String(value)); } catch {} }
      };
      let saved = 0;
      try { saved = parseInt(localStorage.getItem('tmux-side-w') || '', 10); } catch {}
      if (saved) apply(saved, false);
      let drag = null;
      handle.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        event.preventDefault();
        drag = {x: event.clientX, width: sidebar.getBoundingClientRect().width};
        handle.setPointerCapture(event.pointerId);
        document.body.classList.add('resizing');
      });
      handle.addEventListener('pointermove', event => {
        if (drag) apply(drag.width + (event.clientX - drag.x), false);
      });
      const finish = () => {
        if (!drag) return;
        drag = null;
        document.body.classList.remove('resizing');
        apply(sidebar.getBoundingClientRect().width, true);
      };
      handle.addEventListener('pointerup', finish);
      handle.addEventListener('pointercancel', finish);
      handle.addEventListener('dblclick', () => apply(DEFAULT, true));
    }

    // narrow screens start with the drawer shut; wide screens honour the last choice
    if (narrow()) syncCollapsed(true);
    else {
      let collapsed = false;
      try { collapsed = localStorage.getItem('tmux-sidebar-collapsed') === '1'; } catch {}
      syncCollapsed(collapsed);
    }
    window.addEventListener('resize', () => syncCollapsed(state.collapsed));
  }

  async function refreshHost(hostId, button) {
    if (!hostId) return;
    button?.classList.add('busy');
    try {
      const data = await api(`/api/hosts?refresh=1&host=${encodeURIComponent(hostId)}`);
      state.hosts = data.hosts || [];
      renderTree();
      if (state.hooks.onHosts) state.hooks.onHosts(state.hosts);
      toast('已刷新');
    } catch (error) {
      toast(error.message);
    } finally {
      button?.classList.remove('busy');
    }
  }

  // A page may own some of these panes (the multi-window page does); mark those rows so the tree
  // says what is already on screen.
  function markActive(paneIds) {
    const wanted = new Set(paneIds || []);
    for (const row of document.querySelectorAll('#tree [data-pane]')) {
      row.classList.toggle('in-grid', wanted.has(row.dataset.pane));
    }
  }

  window.TmuxShell = {
    markActive,
    mount(hooks = {}) {
      state.hooks = hooks;
      loadOpen();
      bind();
      loadHosts();
      setInterval(() => { if (!document.hidden) loadHosts(); }, 20000);
      setInterval(() => { if (!document.hidden) refreshStates(); }, 10000);
      return {hosts: () => state.hosts, reload: loadHosts, toast};
    },
  };
})();
