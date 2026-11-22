/* 等你回答收件箱：亮灯历史 + 未读数（借鉴清单第 7 项，Open WebUI B4）。
 *
 * 借的是 Open WebUI `routers/notifications.py` 的站内 inbox 形状——游标分页
 * （limit=20）、未读数（/inbox/count）、打开即标已读（/inbox/open）——不借
 * 它的实时推送和多用户模型：实时性复用现有轮询节奏（未读数每
 * 30 秒一轮，抽屉打开时当场拉一页），单用户产品也没有 user/群组。
 *
 * 一条记录 = 一次亮灯：pane、标题、亮灯时间、菜单摘要。卡片消失（等到了
 * 回答 / 超时收起）记录还留在历史里，只多一个“已解决”标记。点一条：
 * pane 还在就跳过去（`/?pane=<id>` 的 selectPane），pane 已关闭就在行内
 * 写“该 pane 已关闭”，不导航、不 404 白屏。
 *
 *   TmuxNeedsYouInbox.start({onJump: pane => selectPane(pane)})
 *   TmuxNeedsYouInbox.open()   // 打开抽屉并标已读
 *   TmuxNeedsYouInbox.stop()
 */
(function () {
  const COUNT_MS = 30000;
  const PAGE = 20;
  // 裸 fetch 没有超时：手机网络一抖、请求挂住，抽屉就一直白板。
  // 15 秒还没回来就当失败，让正文区显示原因而不是无限等。
  const API_TIMEOUT_MS = 15000;

  let onJump = null;
  let timer = null;
  let cursor = null;
  let loading = false;
  let opened = false;
  let readMarked = false;   // 本次打开已标过已读：列表先回来也要把未读点消掉

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  }

  function formatWhen(tsMs) {
    const ms = Number(tsMs);
    if (!ms) return '';
    const diff = Date.now() - ms;
    if (diff < 0) return '刚刚';
    if (diff < 60000) return '刚刚';
    if (diff < 3600000) return Math.floor(diff / 60000) + ' 分钟前';
    if (diff < 86400000) return Math.floor(diff / 3600000) + ' 小时前';
    if (diff < 30 * 86400000) return Math.floor(diff / 86400000) + ' 天前';
    const date = new Date(ms);
    const pad = n => String(n).padStart(2, '0');
    return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate())
      + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  // 服务端 summary 为空时的兜底：标题 + 选项 label，和 needs_you_inbox.py
  // 的 menu_summary 同款形状（标题 — 选项1 / 选项2 …）。
  function menuSummary(prompt) {
    if (!prompt) return '';
    const title = String(prompt.title || '').trim();
    const options = (prompt.options || []).slice(0, 12);
    const labels = options.map(option => {
      const label = String(option.label || '').trim();
      if (!label) return '';
      return option.number ? option.number + '. ' + label : label;
    }).filter(Boolean);
    let body = labels.join(' / ');
    if ((prompt.options || []).length > 12) body += ' 等 ' + prompt.options.length + ' 项';
    return [title, body].filter(Boolean).join(' — ') || String(prompt.kind || '等你回答');
  }

  function itemHtml(item) {
    const summary = item.summary || menuSummary(item);
    const when = formatWhen((item.created_at || 0) * 1000);
    const classes = ['inbox-item'];
    if (!item.read_at) classes.push('unread');
    if (!item.pane_alive) classes.push('closed');
    const status = !item.pane_alive ? '<span class="inbox-closed">该 pane 已关闭</span>'
      : (item.resolved_at ? '<span class="inbox-resolved">已解决</span>' : '');
    return '<button type="button" class="' + classes.join(' ') + '"'
      + ' data-id="' + esc(item.id) + '" data-pane="' + esc(item.pane_id) + '"'
      + ' data-alive="' + (item.pane_alive ? '1' : '0') + '">'
      + '<span class="inbox-dot" aria-hidden="true"></span>'
      + '<span class="inbox-main"><span class="inbox-title">' + esc(item.title || '等你回答') + '</span>'
      + '<span class="inbox-summary">' + esc(summary) + '</span>'
      + '<span class="inbox-meta">' + esc(item.pane_id || '') + ' · ' + esc(when) + ' ' + status + '</span>'
      + '<span class="inbox-note" hidden></span></span></button>';
  }

  function drawer() { return document.getElementById('inbox-drawer'); }
  function backdrop() { return document.getElementById('inbox-backdrop'); }
  function list() { return document.getElementById('inbox-list'); }
  function badge() { return document.getElementById('inbox-badge'); }

  function setBadge(count) {
    const node = badge();
    if (!node) return;
    const n = Number(count) || 0;
    node.hidden = n <= 0;
    node.textContent = n > 99 ? '99+' : String(n);
  }

  function setStatus(text) {
    const node = document.getElementById('inbox-status');
    if (node) node.textContent = text || '';
  }

  // 正文区状态行：载入中 / 没载入必须写在这里，只放底部小字用户看不见，
  // 抽屉看起来就是一张白板（手机截图那次就是这个样子）。
  function setBodyState(text) {
    const node = document.getElementById('inbox-state');
    if (!node) return;
    node.textContent = text || '';
    node.hidden = !text;
  }

  async function api(path, options) {
    const controller = typeof AbortController !== 'undefined'
      ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), API_TIMEOUT_MS) : 0;
    try {
      const response = await fetch(path, Object.assign(
        {credentials: 'same-origin'},
        controller ? {signal: controller.signal} : {},
        options || {}));
      if (response.status === 401) { location = '/login'; throw new Error('登录失效'); }
      if (!response.ok) {
        let detail = {};
        try { detail = await response.json(); } catch (_error) {}
        throw new Error(detail.detail || ('HTTP ' + response.status));
      }
      return response.json();
    } catch (error) {
      if (error && error.name === 'AbortError') {
        throw new Error('请求超时（15 秒），请检查网络后重试');
      }
      throw error;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async function refreshCount() {
    if (document.hidden) return;
    try {
      const data = await api('/api/needs-you/inbox/count');
      setBadge(data.count);
    } catch (_error) {
      // 网络抖动：角标保持旧值，下一轮再说
    }
  }

  // 标已读和拉列表是并行的，列表可能先回来：行按 read_at 渲染时会带上
  // 未读点；标已读一成功就把已渲染行的点消掉，跟角标一致。
  function clearUnreadDots() {
    const box = list();
    if (!box || !box.querySelectorAll) return;
    box.querySelectorAll('.inbox-item.unread').forEach(row => {
      if (row.classList) row.classList.remove('unread');
    });
  }

  async function loadPage(before) {
    if (loading) return;
    const box = list();
    if (!box) return;
    loading = true;
    setStatus('载入中…');
    if (!before) {
      const empty = document.getElementById('inbox-empty');
      if (empty) empty.hidden = true;
      setBodyState('载入中…');
    }
    try {
      const query = '?limit=' + PAGE + (before ? '&before=' + encodeURIComponent(before) : '');
      const data = await api('/api/needs-you/inbox' + query);
      if (!before) box.innerHTML = '';
      const empty = document.getElementById('inbox-empty');
      const html = (data.items || []).map(itemHtml).join('');
      if (!before && !(data.items || []).length) {
        if (empty) empty.hidden = false;
      } else if (empty) {
        empty.hidden = true;
      }
      box.insertAdjacentHTML('beforeend', html);
      cursor = data.next_cursor || null;
      const more = document.getElementById('inbox-more');
      if (more) more.hidden = !cursor;
      if (readMarked) clearUnreadDots();
      setBodyState('');
      // 这个抽屉只列"现在还开着"的；回答过的留在历史里（/api/needs-you/inbox?history=1），
      // 不在这里占位 —— 以前把历史全列出来，看起来就像铃铛在乱报警。
      setStatus((data.items || []).length ? '' : '现在没有正在等你的会话');
    } catch (error) {
      setStatus('没载入：' + error.message);
      // 翻页失败列表里已有内容，正文区不用再写一遍；首屏失败必须写在这里。
      if (!before) setBodyState('没载入：' + error.message);
    } finally {
      loading = false;
    }
  }

  // 打开即标已读（Open WebUI /inbox/open 的语义），但只管发、不等：
  // 标已读挂住不能挡着看列表，失败不挡着看列表，角标下一轮轮询再对。
  async function markRead() {
    try {
      const data = await api('/api/needs-you/inbox/open', {method: 'POST'});
      setBadge(data.count);
      readMarked = true;
      clearUnreadDots();
    } catch (_error) {
      // 标已读失败不挡着看列表：角标下一轮轮询再对
    }
  }

  async function open() {
    const node = drawer();
    if (!node) return;
    opened = true;
    node.hidden = false;
    const shade = backdrop();
    if (shade) shade.hidden = false;
    // 标已读和拉列表并行：以前先 await 标已读再拉列表，手机网络一抖、
    // 标已读请求挂住，列表就永远不拉，抽屉只有静态标题栏。
    cursor = null;
    readMarked = false;
    markRead();
    await loadPage(null);
  }

  function close() {
    const node = drawer();
    if (node) node.hidden = true;
    const shade = backdrop();
    if (shade) shade.hidden = true;
    opened = false;
  }

  function onListClick(event) {
    const row = event.target.closest ? event.target.closest('[data-pane]') : null;
    if (!row) return;
    const pane = row.getAttribute('data-pane');
    const alive = row.getAttribute('data-alive') === '1';
    if (!alive) {
      // pane 已关闭：行内体面说明，不导航，不 404。
      const note = row.querySelector('.inbox-note');
      if (note) {
        note.textContent = '该 pane 已关闭，无法跳转；记录保留在这里备查。';
        note.hidden = false;
      }
      return;
    }
    if (onJump) onJump(pane);
    close();
  }

  function start(options) {
    onJump = (options && options.onJump) || null;
    const button = document.getElementById('inbox-button');
    if (button) button.onclick = () => { if (opened) close(); else open(); };
    const closeButton = document.getElementById('inbox-close');
    if (closeButton) closeButton.onclick = close;
    const shade = backdrop();
    if (shade) shade.onclick = close;
    const more = document.getElementById('inbox-more');
    if (more) more.onclick = () => loadPage(cursor);
    const box = list();
    if (box) box.addEventListener('click', onListClick);
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && opened) close();
    });
    if (timer) clearInterval(timer);
    timer = setInterval(refreshCount, COUNT_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshCount(); });
    refreshCount();
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    close();
  }

  window.TmuxNeedsYouInbox = {start, stop, open, close, refreshCount, itemHtml, formatWhen, menuSummary};
})();
