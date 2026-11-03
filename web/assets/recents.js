/* 「最近」块 —— 控制台 / 主页看板 / 多窗口 共用同一份实现。
 *
 *   TmuxRecents.mount({ onPick, hideWhenFiles })
 *   TmuxRecents.setHosts(hosts) / setStates(map) / markActive(paneIds) / refresh()
 *
 * 三处侧栏只有「点了之后干什么」不一样：控制台切到那个会话，多窗口把它加成一格，
 * 主页跳到控制台。其余（清单、钉子、收藏、改名、高度、折叠）全在这里，别再各写一份。
 */
(() => {
  const H_KEY = 'tmux-recent-h', FOLD_KEY = 'tmux-recent-fold';
  const H_DEFAULT = 0.4, H_MIN = 0.15, H_MAX = 0.75, MAX_ROWS = 8;

  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));

  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: 'same-origin', headers: {'Accept': 'application/json'}, ...options,
    });
    if (response.status === 401) { location = '/login'; throw new Error('登录失效'); }
    if (!response.ok) {
      let detail = {};
      try { detail = await response.json(); } catch {}
      throw new Error((detail && detail.detail) || `HTTP ${response.status}`);
    }
    return response.json();
  }

  // 三个页面各有自己的 SVG sprite；缺的这几个图标由这里补，页面不用再抄一遍
  const ICONS = {
    'i-chevron': '<path d="m6.5 9.5 5.5 5.5 5.5-5.5"/>',
    'i-pin': '<path d="M9.5 3.5h5M12 3.5v6.2M8.4 9.7h7.2l1.4 3.4H7l1.4-3.4ZM12 13.1v7.4"/>',
    'i-pin-fill': '<path d="M9.5 3.5h5M12 3.5v6.2M8.4 9.7h7.2l1.4 3.4H7l1.4-3.4Z" fill="currentColor" stroke="currentColor"/><path d="M12 13.1v7.4"/>',
    'i-pencil': '<path d="M4.6 19.4h3.2L19 8.2a2.1 2.1 0 0 0-3-3L4.8 16.4Z"/><path d="m14.6 6.6 3 3"/>',
    'i-star': '<path d="m12 4.6 2.4 5 5.5.8-4 3.9.9 5.5-4.8-2.6-4.8 2.6.9-5.5-4-3.9 5.5-.8z"/>',
    'i-star-fill': '<path d="m12 4.6 2.4 5 5.5.8-4 3.9.9 5.5-4.8-2.6-4.8 2.6.9-5.5-4-3.9 5.5-.8z" fill="currentColor"/>',
    'i-close': '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  };
  function ensureIcons() {
    let sprite = document.getElementById('tmux-recents-sprite');
    const missing = Object.keys(ICONS).filter(id => !document.getElementById(id));
    if (!missing.length) return;
    if (!sprite) {
      sprite = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      sprite.setAttribute('id', 'tmux-recents-sprite');
      sprite.setAttribute('width', '0');
      sprite.setAttribute('height', '0');
      sprite.setAttribute('aria-hidden', 'true');
      sprite.style.position = 'absolute';
      sprite.innerHTML = '<defs></defs>';
      document.body.appendChild(sprite);
    }
    const defs = sprite.querySelector('defs');
    for (const id of missing) {
      defs.insertAdjacentHTML('beforeend',
        `<symbol id="${id}" viewBox="0 0 24 24">${ICONS[id]}</symbol>`);
    }
  }

  const state = {
    entries: [], hosts: [], states: new Map(), favorites: new Set(),
    hooks: {}, block: null, list: null, signature: '', lastAt: 0,
    renaming: null, drag: null, suppressClickAt: 0, mounted: false,
  };

  // ---------------------------------------------------------------- storage
  const readNumber = (key, fallback, min, max) => {
    try {
      const value = Number(localStorage.getItem(key));
      if (Number.isFinite(value) && value >= min && value <= max) return value;
    } catch {}
    return fallback;
  };

  function applyRatio(ratio) {
    if (!state.block) return;
    const clamped = Math.min(H_MAX, Math.max(H_MIN, ratio));
    state.block.style.setProperty('--recent-h', `${(clamped * 100).toFixed(2)}%`);
  }

  function folderState() {
    try { return localStorage.getItem(FOLD_KEY) === '1'; } catch { return false; }
  }

  function setFolded(on, persist) {
    if (!state.block) return;
    const was = state.block.classList.contains('collapsed');
    state.block.classList.toggle('collapsed', !!on);
    // 折叠期间浏览器记着滚动位置，展开后会把固定区推出视野——看起来像"钉子没了"
    if (!on && was && state.list) state.list.scrollTop = 0;
    const button = $('recent-fold');
    if (button) {
      button.setAttribute('aria-expanded', on ? 'false' : 'true');
      const title = on ? '展开最近' : '折叠最近';
      button.title = title;
      button.setAttribute('aria-label', title);
    }
    if (persist) { try { localStorage.setItem(FOLD_KEY, on ? '1' : '0'); } catch {} }
  }

  // ---------------------------------------------------------------- lookups
  function paneById(id) {
    for (const host of state.hosts) {
      for (const pane of host.panes || []) {
        if (pane.id === id) return {pane, host};
      }
    }
    return null;
  }

  // 一个 pane 的“人起的名字”：钉子上的 → 收藏处的（没有就空串）
  function nameFor(id) {
    const entry = state.entries.find(item => item.pane === id);
    if (!entry) return '';
    return (entry.label || '').trim() || (entry.title || '').trim();
  }

  function labelFor(id) {
    const found = paneById(id);
    if (!found) return null;
    const {pane, host} = found;
    const win = `${pane.session}${pane.window_name ? ': ' + pane.window_name : ''}`;
    return `${host.name || host.id} · ${win}`;
  }

  function relativeTime(stamp) {
    const seconds = Math.max(0, Math.round(Date.now() / 1000 - Number(stamp || 0)));
    if (!seconds) return '';
    if (seconds < 60) return '刚刚';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时`;
    return `${Math.floor(seconds / 86400)} 天`;
  }

  const filesOpen = () =>
    state.hooks.hideWhenFiles && (() => {
      const panel = $('file-panel');
      if (!panel || panel.hidden) return false;
      return true;
    })();

  // ---------------------------------------------------------------- render
  function render() {
    if (!state.block || !state.list) return;
    const rows = [];
    let recentCount = 0;
    for (const entry of state.entries) {
      const known = !!paneById(entry.pane);
      if (!known && !entry.pinned) continue;      // 固定项即使主机不在也留着（有记下的名字）
      if (!entry.pinned) {
        if (recentCount >= MAX_ROWS) continue;
        recentCount += 1;
      }
      rows.push(entry);
    }
    if (filesOpen() || !rows.length) {
      state.block.hidden = true;
      state.list.innerHTML = '';
      state.signature = '';
      return;
    }
    state.block.hidden = false;
    const signature = rows.map(row =>
      `${row.pane}|${row.pinned ? 'p' : ''}|${row.label || ''}|${row.title || ''}|${row.favorite ? 'f' : ''}`).join(',');
    // 结构没变时只刷新高亮和圆点：重建会把编辑中的输入框冲掉，也会让双击的第二下打空
    if (signature === state.signature && state.list.children.length && !state.renaming) {
      refreshLiveBits();
      return;
    }
    state.signature = signature;
    const keepScroll = state.list.scrollTop;
    const html = rows.map(rowHtml);
    const pinned = rows.filter(row => row.pinned);
    const rest = rows.filter(row => !row.pinned);
    const pinnedHtml = pinned.length
      ? `<div class="recent-pins">${pinned.map(rowHtml).join('')}</div>` : '';
    state.list.innerHTML = pinnedHtml + rest.map(rowHtml).join('');
    void html;
    state.list.scrollTop = keepScroll;
    refreshLiveBits();
    if (state.hooks.onNames) state.hooks.onNames();     // 顶栏标题跟着名字走
  }

  function refreshLiveBits() {
    for (const row of state.list.querySelectorAll('.recent-row')) {
      const found = paneById(row.dataset.row);
      row.querySelector('.dot').className = 'dot ' +
        (found ? (state.states.get(row.dataset.row) || '') : 'offline');
      const star = row.querySelector('[data-star]');
      if (star) {
        const on = state.favorites.has(row.dataset.row);
        star.classList.toggle('on', on);
        star.setAttribute('aria-pressed', on ? 'true' : 'false');
        star.title = on ? '取消收藏' : '收藏到主页';
        star.setAttribute('aria-label', star.title);
        const use = star.querySelector('use');
        if (use) use.setAttribute('href', on ? '#i-star-fill' : '#i-star');
      }
    }
    const button = $('recent-pin-selected');
    if (button) {
      const active = state.block.dataset.active || '';
      const entry = state.entries.find(item => item.pane === active);
      const on = !!entry && entry.pinned;
      button.disabled = !active;
      button.classList.toggle('on', on);
      button.setAttribute('aria-pressed', on ? 'true' : 'false');
      const title = on ? '取消固定当前会话' : '固定当前会话';
      button.title = title;
      button.setAttribute('aria-label', title);
      const use = button.querySelector('use');
      if (use) use.setAttribute('href', on ? '#i-pin-fill' : '#i-pin');
    }
  }

  function rowHtml(entry) {
    const found = paneById(entry.pane);
    const fallback = found ? labelFor(entry.pane) : entry.pane;
    // 名字优先级：钉子自己的名字 → 主页收藏处的名字 → 会话/窗口名
    // （服务端已经把收藏的 title 一起带下来了，钉了但没单独改名的行不会退化成 claude · 0: main · oracle）
    const custom = (entry.label || '').trim();
    const favorite_name = (entry.title || '').trim();
    const label = custom || favorite_name || fallback;
    const status = found ? (state.states.get(entry.pane) || '') : 'offline';
    const active = entry.pane === state.block.dataset.active ? ' active' : '';
    const on = !!entry.pinned;
    const starred = state.favorites.has(entry.pane);
    const hint = on ? `${label} · ${entry.pane} · 双击改名` : `${fallback} · ${entry.pane}`;
    return `<div class="recent-row${on ? ' pinned' : ''}${active}" data-row="${esc(entry.pane)}">
      <button class="recent-open" type="button" data-recent="${esc(entry.pane)}" title="${esc(hint)}">
        <span class="pane-num">${entry.number ? '#' + entry.number : ''}</span>
        <span class="dot ${esc(status)}"></span>
        <span class="label">${esc(label)}</span>
        ${on ? '' : `<span class="when">${relativeTime(entry.visited_at)}</span>`}
      </button>
      <button class="recent-act${on ? ' on' : ''}" type="button" data-pin="${esc(entry.pane)}" aria-pressed="${on ? 'true' : 'false'}"
        title="${on ? '取消固定' : '固定到上面'}" aria-label="${on ? '取消固定' : '固定到上面'}">
        <svg class="ico"><use href="${on ? '#i-pin-fill' : '#i-pin'}"/></svg>
      </button>
      ${on ? `<button class="recent-act" type="button" data-rename="${esc(entry.pane)}" title="改名" aria-label="改名">
        <svg class="ico"><use href="#i-pencil"/></svg>
      </button>` : ''}
      <button class="recent-act${starred ? ' on' : ''}" type="button" data-star="${esc(entry.pane)}"
        aria-pressed="${starred ? 'true' : 'false'}" title="${starred ? '取消收藏' : '收藏到主页'}"
        aria-label="${starred ? '取消收藏' : '收藏到主页'}">
        <svg class="ico"><use href="${starred ? '#i-star-fill' : '#i-star'}"/></svg>
      </button>
    </div>`;
  }

  // ---------------------------------------------------------------- data
  let lastAt = 0;
  async function load() {
    state.renaming = null;
    lastAt = Date.now();
    try {
      const [recent, favorites] = await Promise.all([
        api('/api/home/recent?limit=12'),
        api('/api/home/favorites').catch(() => ({favorites: []})),
      ]);
      state.entries = (recent.recent || []).filter(entry => entry && entry.pane).map(entry => ({
        pane: entry.pane,
        number: Number(entry.number) || 0,
        label: entry.label || '',
        title: entry.title || '',
        pinned: !!entry.pinned,
        favorite: !!entry.favorite,
        visited_at: Number(entry.visited_at) || 0,
      }));
      state.favorites = new Set((favorites.favorites || []).map(item =>
        typeof item === 'string' ? item : item.pane).filter(Boolean));
      render();
    } catch {
      render();
    }
  }

  // 记一次访问：服务端留档，本地把这条顶到普通"最近"的第一位（钉住的只更新时间）
  async function noteVisit(pane) {
    if (!pane) return;
    const existing = state.entries.find(entry => entry.pane === pane);
    const stamp = Math.round(Date.now() / 1000);
    if (existing && existing.pinned) {
      existing.visited_at = stamp;
    } else {
      state.entries = [
        ...state.entries.filter(entry => entry.pinned),
        {...(existing || {pane}), pane, visited_at: stamp},
        ...state.entries.filter(entry => !entry.pinned && entry.pane !== pane),
      ].slice(0, 24);
    }
    render();
    try {
      await fetch('/api/home/recent', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pane}), keepalive: true,
      });
    } catch {}
  }

  function entryFor(pane) { return state.entries.find(item => item.pane === pane); }

  async function togglePin(pane) {
    const entry = entryFor(pane);
    const on = !!entry && entry.pinned;
    try {
      await api('/api/home/recent/pin', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pane, pinned: !on, label: (entry && entry.label) || ''}),
      });
    } catch (error) { toast(error.message); return; }
    if (!on) {
      state.entries = [
        {...(entry || {pane}), pane, pinned: true, visited_at: (entry && entry.visited_at) || Math.round(Date.now() / 1000)},
        ...state.entries.filter(item => item.pane !== pane),
      ];
    } else {
      state.entries = [
        ...state.entries.filter(item => item.pinned && item.pane !== pane),
        {...(entry || {pane}), pane, pinned: false, label: '', visited_at: Math.round(Date.now() / 1000)},
        ...state.entries.filter(item => !item.pinned && item.pane !== pane),
      ];
    }
    render();
    if (!on && state.list) state.list.scrollTop = 0;   // 刚钉的在最上面
  }

  async function toggleStar(pane) {
    const on = state.favorites.has(pane);
    try {
      if (on) {
        await api(`/api/home/favorites/${encodeURIComponent(pane)}`, {method: 'DELETE'});
        state.favorites.delete(pane);
      } else {
        await api('/api/home/favorites', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({pane}),
        });
        state.favorites.add(pane);
      }
      render();
      // 会话树自己也有星，别处要跟着变
      window.dispatchEvent(new CustomEvent('tmuxweb:favorites', {detail: {pane, on: !on}}));
    } catch (error) { toast(error.message); }
  }

  // ---- 改名：双击（桌面）/ 长按（触屏）/ 铅笔按钮
  function startRename(pane) {
    if (state.renaming) return;
    const row = state.list.querySelector(`.recent-row[data-row="${CSS.escape(pane)}"]`);
    if (!row || !row.classList.contains('pinned')) return;
    const label = row.querySelector('.label');
    if (!label) return;
    const entry = entryFor(pane) || {};
    const input = document.createElement('input');
    input.className = 'recent-rename';
    input.type = 'text';
    input.maxLength = 80;
    input.value = (entry.label || label.textContent || '').trim();
    input.setAttribute('aria-label', '改名');
    input.spellcheck = false;
    const mine = {pane, done: false, commit: null};   // 每次改名一个自己的标记：换了行别互相打架
    state.renaming = mine;
    row.classList.add('renaming');
    label.replaceWith(input);
    input.focus();
    input.select();
    const finish = save => {
      if (mine.done) return;
      mine.done = true;
      const value = input.value;
      if (state.renaming === mine) state.renaming = null;
      state.signature = '';                    // 强制重建，把 input 换回文本
      if (!save) { render(); return; }
      const current = entryFor(pane);
      if (!current) { render(); return; }
      const clean = (value || '').trim().slice(0, 80);
      current.label = clean;
      if (current.favorite) current.title = clean;   // 只有"主动改名"会让两边同名
      render();
      api(`/api/home/recent/pin/${encodeURIComponent(pane)}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({label: clean}),
      }).then(() => load()).catch(error => { toast(error.message); load(); });
    };
    mine.commit = finish;
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); finish(true); }
      else if (event.key === 'Escape') { event.preventDefault(); finish(false); }
      event.stopPropagation();
    });
    input.addEventListener('blur', () => finish(true));
    input.addEventListener('pointerdown', event => event.stopPropagation());
  }

  function toast(text) {
    const el = document.querySelector('.shell-toast');
    if (el) {
      el.textContent = text;
      el.classList.add('show');
      clearTimeout(toast.timer);
      toast.timer = setTimeout(() => el.classList.remove('show'), 1800);
      return;
    }
    if (state.hooks.toast) state.hooks.toast(text);
    else console.warn(text);
  }

  // ---------------------------------------------------------------- wiring
  function bind() {
    state.list.addEventListener('click', event => {
      if (Date.now() - state.suppressClickAt < 600) { event.preventDefault(); return; }
      if (state.renaming) return;
      const rename = event.target.closest('[data-rename]');
      if (rename) { event.preventDefault(); startRename(rename.dataset.rename); return; }
      const pin = event.target.closest('[data-pin]');
      if (pin) { event.preventDefault(); togglePin(pin.dataset.pin); return; }
      const star = event.target.closest('[data-star]');
      if (star) { event.preventDefault(); toggleStar(star.dataset.star); return; }
      const row = event.target.closest('[data-recent]');
      if (!row) return;
      event.preventDefault();
      const pane = row.dataset.recent;
      // 页面自己决定点击做什么（切会话 / 加窗口 / 跳控制台），顺便用 touch() 记一次访问
      if (state.hooks.onPick) state.hooks.onPick(pane, row);
    });

    state.list.addEventListener('dblclick', event => {
      const open = event.target.closest('.recent-open');
      const row = open && open.closest('.recent-row.pinned');
      if (!row) return;
      event.preventDefault();
      startRename(row.dataset.row);
    });

    // 钉子 / 改名 / 收藏：在 pointerdown 上就执行。
    // 原因：改名的输入框一旦失焦会 blur → 保存 → 重绘，重绘会把这次 click 吞掉，
    // 表现就是"第二次点铅笔没反应"。
    let pressTimer = 0;
    state.list.addEventListener('pointerdown', event => {
      const act = event.target.closest('[data-rename],[data-pin],[data-star]');
      if (act) {
        event.preventDefault();                  // 别把焦点交给按钮：刚建出来的输入框会被 blur 掉
        const pending = state.renaming;
        if (pending && !pending.done && pending.commit) pending.commit(true);   // 先把上一个改名存掉
        state.suppressClickAt = Date.now();      // 紧随其后的 click 不要再触发一遍
        if (act.dataset.rename) startRename(act.dataset.rename);
        else if (act.dataset.pin) togglePin(act.dataset.pin);
        else if (act.dataset.star) toggleStar(act.dataset.star);
        return;
      }
      const open = event.target.closest('.recent-open');
      if (!open || event.pointerType === 'mouse') return;
      const row = open.closest('.recent-row.pinned');
      if (!row) return;
      const pane = row.dataset.row;
      clearTimeout(pressTimer);
      pressTimer = setTimeout(() => { state.suppressClickAt = Date.now(); startRename(pane); }, 480);
    });
    for (const type of ['pointerup', 'pointercancel', 'pointermove']) {
      state.list.addEventListener(type, () => clearTimeout(pressTimer), {passive: true});
    }
    state.list.addEventListener('contextmenu', event => {
      if (event.target.closest('.recent-row.pinned')) event.preventDefault();
    });

    $('recent-fold')?.addEventListener('click', () => {
      setFolded(!state.block.classList.contains('collapsed'), true);
    });

    const grip = $('recent-grip');
    grip?.addEventListener('pointerdown', event => {
      if (state.block.hidden || state.block.classList.contains('collapsed')) return;
      event.preventDefault();
      const parent = state.block.parentElement;
      const total = parent ? parent.getBoundingClientRect().height : window.innerHeight;
      state.drag = {startY: event.clientY, startH: state.block.getBoundingClientRect().height, total};
      grip.classList.add('dragging');
      document.body.classList.add('recent-resizing');
      if (grip.setPointerCapture) grip.setPointerCapture(event.pointerId);
    });
    window.addEventListener('pointermove', event => {
      if (!state.drag) return;
      const height = state.drag.startH - (event.clientY - state.drag.startY);   // 往上拖 = 变高
      applyRatio(height / Math.max(1, state.drag.total));
    });
    const endDrag = () => {
      if (!state.drag) return;
      const ratio = state.block.getBoundingClientRect().height / Math.max(1, state.drag.total);
      try { localStorage.setItem(H_KEY, String(Math.min(H_MAX, Math.max(H_MIN, ratio)))); } catch {}
      state.drag = null;
      grip.classList.remove('dragging');
      document.body.classList.remove('recent-resizing');
    };
    window.addEventListener('pointerup', endDrag);
    window.addEventListener('pointercancel', endDrag);
    grip?.addEventListener('dblclick', () => {
      applyRatio(H_DEFAULT);
      try { localStorage.removeItem(H_KEY); } catch {}
    });

    $('recent-pin-selected')?.addEventListener('click', () => {
      const pane = state.block.dataset.active;
      if (pane) togglePin(pane);
    });
    $('recent-clear')?.addEventListener('click', async () => {
      const panes = state.entries.filter(entry => !entry.pinned).map(entry => entry.pane);
      state.entries = state.entries.filter(entry => entry.pinned);
      render();
      for (const pane of panes) {
        try { await fetch(`/api/home/recent/${encodeURIComponent(pane)}`, {method: 'DELETE'}); } catch {}
      }
    });

    // 切回标签页/窗口时补一次，页面常开每 2 分钟对一次
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && Date.now() - lastAt > 15000) load();
    });
    window.addEventListener('focus', () => {
      if (Date.now() - lastAt > 15000) load();
    });
    setInterval(() => { if (!document.hidden) load(); }, 120000);

    // 文件面板打开时这块让位：控制台就是这么用的
    const panel = $('file-panel');
    if (panel && state.hooks.hideWhenFiles) {
      new MutationObserver(render).observe(panel, {attributes: true, attributeFilter: ['hidden']});
    }
  }

  function markup() {
    const section = document.createElement('section');
    section.className = 'recent';
    section.id = 'recent-block';
    section.hidden = true;
    section.setAttribute('aria-label', '最近的会话');
    section.innerHTML = `
      <div class="recent-grip" id="recent-grip" role="separator" aria-label="拖动调整最近的高度"
        title="拖动调整高度，双击恢复 2/5"></div>
      <div class="recent-head">
        <button class="recent-fold" id="recent-fold" type="button" aria-expanded="true"
          title="折叠最近" aria-label="折叠最近"><svg class="ico"><use href="#i-chevron"/></svg></button>
        <span class="recent-title">最近</span>
        <span class="recent-tools">
          <button class="recent-clear" id="recent-pin-selected" type="button"
            title="固定当前会话" aria-label="固定当前会话" aria-pressed="false" disabled>
            <svg class="ico"><use href="#i-pin"/></svg></button>
          <button class="recent-clear" id="recent-clear" type="button"
            title="清空最近记录（固定的不动）" aria-label="清空最近记录">
            <svg class="ico"><use href="#i-close"/></svg></button>
        </span>
      </div>
      <div class="recent-list" id="recent-list"></div>`;
    return section;
  }

  function mount(hooks = {}) {
    if (state.mounted) return window.TmuxRecents;
    const sidebar = document.getElementById(hooks.container || 'sidebar');
    if (!sidebar) return window.TmuxRecents;
    ensureIcons();
    state.hooks = hooks;
    const section = markup();
    // 放在树下面、文件面板/页脚（侧栏底）之前
    const before = $('file-panel') || sidebar.querySelector('.side-resizer') ||
      sidebar.querySelector('.side-foot') || null;
    sidebar.insertBefore(section, before && before.parentElement === sidebar ? before : null);
    state.block = section;
    state.list = $('recent-list');
    applyRatio(readNumber(H_KEY, H_DEFAULT, H_MIN, H_MAX));
    setFolded(folderState(), false);
    bind();
    load();
    state.mounted = true;
    return window.TmuxRecents;
  }

  window.TmuxRecents = {
    mount,
    nameFor,
    refresh: load,
    touch: noteVisit,
    setFavorites(list) {
      state.favorites = new Set((list || []).map(item =>
        typeof item === 'string' ? item : item.pane).filter(Boolean));
      render();
    },
    setHosts(hosts) { state.hosts = hosts || []; render(); },
    setStates(map) {
      state.states = map instanceof Map ? map : new Map(Object.entries(map || {}));
      render();
    },
    markActive(panes) {
      const list = Array.isArray(panes) ? panes : (panes ? [panes] : []);
      if (!state.block) return;
      state.block.dataset.active = list[0] || '';
      for (const row of document.querySelectorAll('.recent-row')) {
        row.classList.toggle('active', list.includes(row.dataset.row));
      }
      refreshLiveBits();
    },
    toggle(pane) { return togglePin(pane); },
  };
})();
