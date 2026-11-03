/* 「悬停预览」面板 —— home 卡片和侧栏共用一份实现。
 *
 * 数据来自 /api/home（favorites / recent 的 agent_preview、preview、status、
 * cached），所以首页卡片和侧栏行看到的是同一条最后消息，不各写一套取数逻辑。
 *
 *   TmuxPanePreview.mount({
 *     data:   () => Map | {paneId: entry},   // 由宿主喂 /api/home 的条目
 *     delay:  () => 2000,                    // 悬停多久才弹；0 = 不延迟，null = 关闭
 *     render: text => html,                  // 宿主自己的 markdown 渲染器
 *     onChat: pane => …, onFull: pane => …,  // 面板底部两个按钮
 *   });
 *   TmuxPanePreview.bind(container, {selector, paneOf, stickyClick})
 *
 * 手机 web 默认关闭：只有 (hover:hover) and (pointer:fine) 才绑定，
 * 触摸设备上悬停预览本来就会挡住第一次点击。
 */
(function () {
  const canHover = !!(window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches);

  const options = { data: null, delay: () => 2000, render: null, onChat: null, onFull: null, force: false, fetch: null };
  // 不在 /api/home 里的 pane（会话树里大多数行）：悬停时按需取一次，60 秒内复用
  const fetched = new Map();
  const FETCH_TTL = 60_000;
  let mounted = false;
  let panel = null;
  let body = null;
  let roleChip = null;
  let timeLabel = null;
  let currentPane = null;
  let sticky = false;
  let showTimer = 0;
  let hideTimer = 0;

  function ago(ts) {
    if (!ts) return '';
    const seconds = (Date.now() / 1000) - Number(ts);
    if (!isFinite(seconds) || seconds < 0) return '';
    if (seconds < 60) return '刚刚';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    return `${Math.floor(seconds / 86400)} 天前`;
  }

  function entryFor(paneId) {
    if (!options.data) return null;
    const data = options.data();
    if (!data) return null;
    if (typeof data.get === 'function') return data.get(paneId) || null;
    return data[paneId] || null;
  }

  // 一个会话可能只有 preview、只有 agent_preview，或者什么都没有：
  // 顺序和 home 卡片一致，最后给一句说明，面板永远不会是空白。
  function previewFor(paneId) {
    const entry = entryFor(paneId) || {};
    const offline = entry.status === 'offline';
    const cached = !!entry.cached;
    const source = entry.agent_preview || entry.preview;
    if (source) return { ...source, offline, cached, cachedAt: entry.cached_at };
    return {
      role: entry.provider || 'pane',
      text: cached ? '缓存里也没有这个会话的内容。' : '这个会话暂时没有可预览的内容。',
      timestamp: null,
      offline,
      cached,
      cachedAt: entry.cached_at,
    };
  }

  function build() {
    if (panel) return panel;
    panel = document.createElement('div');
    panel.className = 'hover-preview';
    panel.id = 'hover-preview';
    panel.hidden = true;
    panel.innerHTML = ''
      + '<div class="hp-head"><span id="hp-role">agent</span><span id="hp-time"></span>'
      + '<button class="hp-x" id="hp-x" type="button" title="关闭预览" aria-label="关闭预览">'
      + '<svg class="ico" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>'
      + '<div class="hp-body" id="hp-body"></div>'
      + '<div class="hp-actions">'
      + '<button class="hp-btn" type="button" data-hp="full">完整预览</button>'
      + '<button class="hp-btn primary" type="button" data-hp="chat">聊天</button>'
      + '<button class="hp-btn" type="button" data-hp="close">关闭</button>'
      + '</div>';
    body = panel.querySelector('#hp-body');
    roleChip = panel.querySelector('#hp-role');
    timeLabel = panel.querySelector('#hp-time');
    panel.addEventListener('pointerenter', cancelHide);
    panel.addEventListener('pointerleave', scheduleHide);
    panel.addEventListener('click', event => {
      if (event.target.closest('#hp-x')) { event.preventDefault(); hide(); return; }
      const action = event.target.closest('[data-hp]');
      if (!action || !currentPane) return;
      event.preventDefault();
      event.stopPropagation();
      const pane = currentPane;
      const kind = action.dataset.hp;
      if (kind === 'chat' && options.onChat) { hide(); options.onChat(pane); }
      else if (kind === 'full' && options.onFull) { hide(); options.onFull(pane); }
      else hide();
    });
    document.body.appendChild(panel);
    document.addEventListener('keydown', event => { if (event.key === 'Escape' && !panel.hidden) hide(); });
    window.addEventListener('resize', () => hide());
    window.addEventListener('scroll', event => {
      if (!panel.hidden && !panel.contains(event.target)) hide();
    }, { passive: true, capture: true });
    return panel;
  }

  function narrow() {
    return window.matchMedia('(max-width: 860px)').matches;
  }

  function place(anchor) {
    if (!anchor || !anchor.getBoundingClientRect) return;
    if (narrow()) { panel.style.left = ''; panel.style.top = ''; panel.style.width = ''; return; }
    const rect = anchor.getBoundingClientRect();
    const viewportW = window.innerWidth;
    const viewportH = window.innerHeight;
    const width = Math.min(520, viewportW - 24);
    panel.style.width = `${width}px`;
    let left = rect.right + 12;                       // 优先放在右边：侧栏行在左侧
    if (left + width > viewportW - 12) left = Math.max(12, rect.left - width - 12);
    const height = Math.min(panel.scrollHeight || 260, viewportH * 0.62);
    const top = Math.min(Math.max(12, rect.top), Math.max(12, viewportH - height - 12));
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
  }

  function render(preview) {
    roleChip.textContent = preview.role === 'assistant' ? 'agent 回复' : (preview.role || 'pane');
    const origin = preview.offline ? (preview.cached ? '离线 · 缓存' : '离线') : '';
    timeLabel.textContent = [origin, ago(preview.timestamp), preview.truncated ? '已截断' : '']
      .filter(Boolean).join(' · ');
    panel.classList.toggle('offline', !!preview.offline);
    panel.classList.toggle('cached', !!preview.cached);
    if (options.render) {
      body.innerHTML = options.render(preview.text);
      body.classList.add('md');
    } else {
      body.textContent = preview.text;
      body.classList.remove('md');
    }
  }

  /** 把 /api/panes/<pane>/transcript 的结果收成一条预览（宿主的按需取数用） */
  function previewFromTranscript(data) {
    const messages = (data && data.messages) || [];
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message && message.text) {
        return {
          role: message.role || 'assistant',
          text: message.text,
          timestamp: message.timestamp ? Date.parse(message.timestamp) / 1000 : null,
          truncated: false,
        };
      }
    }
    return null;
  }

  function cachedFetch(paneId) {
    const hit = fetched.get(paneId);
    if (hit && Date.now() - hit.at < FETCH_TTL) return hit.value;
    return null;
  }

  function show(anchor, paneId, settings) {
    if (!paneId) return;
    let preview = previewFor(paneId);
    const entry = entryFor(paneId);
    const empty = !entry || !(entry.agent_preview || entry.preview);
    if (empty && options.fetch) {
      const cached = cachedFetch(paneId);
      if (cached) {
        preview = { ...cached, offline: false, cached: false };
      } else {
        // 先显示"读取中"，取回来再填；面板已经弹出，不会闪
        preview = { role: entry && entry.provider || 'pane', text: '读取中…', timestamp: null, offline: false, cached: false };
        cancelHide();
        build();
        currentPane = paneId;
        render(preview);
        panel.hidden = false;
        place(anchor);
        Promise.resolve(options.fetch(paneId))
          .then(result => {
            if (result) fetched.set(paneId, { at: Date.now(), value: result });
            if (currentPane !== paneId || panel.hidden) return;
            render(result ? { ...result, offline: false, cached: false } : previewFor(paneId));
            place(anchor);
          })
          .catch(() => {
            if (currentPane === paneId) render(previewFor(paneId));
          });
        return;
      }
    }
    if (!preview) return;
    cancelHide();
    build();
    const keepSticky = !!(settings && settings.sticky);
    if (!panel.hidden && currentPane === paneId && !keepSticky) { place(anchor); return; }
    sticky = keepSticky || sticky;
    currentPane = paneId;
    render(preview);
    panel.hidden = false;
    place(anchor);
  }

  function hide() {
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    if (panel) panel.hidden = true;
    currentPane = null;
    sticky = false;
  }

  function cancelHide() {
    clearTimeout(hideTimer);
  }

  function scheduleHide() {
    if (sticky) return;                     // 点击打开的预览要明确关掉
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, 260);
  }

  /** 悬停多久才弹；null = 用户在设置里关掉了 */
  function delaySetting() {
    if (!options.delay) return 2000;
    const value = options.delay();
    if (value === null || value === false || value === 'off') return null;
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return 2000;
    return number;
  }

  /* 把一组元素绑成「悬停 N 秒出预览」。
   * selector 决定哪些行、paneOf 从行里取 pane id；同一个容器可以绑多次（去重）。 */
  function bind(container, config) {
    if (!container || container.dataset.panePreviewBound === '1') return;
    // 手机 web：默认关闭（没有悬停，预览会挡住第一次点击）。
    // force 是给"浏览器报错 hover 能力"或调试用的逃生口（?preview=1）。
    if (!canHover && !options.force) return;
    container.dataset.panePreviewBound = '1';
    const selector = (config && config.selector) || '[data-pane]';
    const paneOf = (config && config.paneOf) || (element => element.dataset.pane || element.dataset.recent || element.dataset.row);
    const stickyClick = !!(config && config.stickyClick);

    container.addEventListener('pointerover', event => {
      if (event.pointerType && event.pointerType !== 'mouse') return;
      const row = event.target.closest && event.target.closest(selector);
      if (!row || !container.contains(row)) return;
      const paneId = paneOf(row);
      if (!paneId) return;
      clearTimeout(showTimer);
      const wait = delaySetting();
      if (wait === null) return;                      // 设置里关掉了
      if (wait <= 0) { show(row, paneId); return; }
      showTimer = setTimeout(() => show(row, paneId), wait);
    });
    container.addEventListener('pointerout', event => {
      if (event.pointerType && event.pointerType !== 'mouse') return;
      const row = event.target.closest && event.target.closest(selector);
      if (!row) return;
      if (event.relatedTarget && row.contains(event.relatedTarget)) return;
      clearTimeout(showTimer);
      scheduleHide();
    });
    if (stickyClick) {
      container.addEventListener('click', event => {
        const row = event.target.closest && event.target.closest(selector);
        if (!row) return;
        const paneId = paneOf(row);
        if (paneId) show(row, paneId, { sticky: true });
      });
    }
  }

  function mount(config) {
    Object.assign(options, config || {});
    mounted = true;
    build();
    return { show, hide, bind, canHover, mounted: () => mounted };
  }

  window.TmuxPanePreview = {
    mount,
    get hoverable() { return canHover || !!options.force; },
    bind,
    show,
    hide,
    canHover,
    ago,
    /** 面板是否正在显示 / 显示的是哪个 pane（测试与调试用） */
    state: () => ({ visible: !!(panel && !panel.hidden), pane: currentPane, sticky }),
    delaySetting,
    previewFromTranscript,
  };
})();
