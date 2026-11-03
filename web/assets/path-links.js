/* Path links for chat text — an independent post-processing step.
 *
 * Chat/agent output constantly mentions `/some/dir` or `report.md` without marking it up.
 * This module walks a rendered subtree, turns path-looking text into `<a class="plink">`
 * and decides what a click means (only then asking the server, so rendering stays cheap):
 *
 *   directory  -> options.onDir(path)
 *   markdown   -> options.onMd(path)          (falls back to onFile)
 *   file       -> options.onFile(path)
 *   missing    -> options.onMissing(path)
 *
 *   TmuxPaths.enhance(root, {pane, cwd, onDir, onFile, onMd, onMissing})
 *
 * Safe by construction: never touches <a>, <pre>, <code>, <script>, <style>, inputs or
 * anything already marked data-plink.
 */
(function () {
  // 绝对/家目录路径，或"带扩展名的相对路径"（results/out.md、src/main.py）。
  // 后者故意要求"斜杠 + 扩展名"，免得把 and/or 之类的普通词也变成链接。
  const BOUNDARY = "[\\s(（\\[【<\"'`：:，、；;！？!?）】」』》>]";
  const ABSOLUTE = new RegExp('(^|' + BOUNDARY + ')((?:~|\\.{1,2})?\\/[^\\s\"' + '`<>|，。；：、）)】]+)', 'g');
  const RELATIVE = new RegExp('(^|' + BOUNDARY + ')((?:\\.{1,2}\\/)?(?:[\\w@%+.-]+\\/)+[\\w@%+.-]+\\.[A-Za-z0-9]{1,8})', 'g');
  const SKIP_TAGS = new Set(['A', 'PRE', 'SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'BUTTON', 'SVG']);
  const MD_EXT = /\.(md|markdown|mdx)$/i;
  // 可以直接看内容、也可以下载的文本类
  const TEXT_EXT = /\.(md|markdown|mdx|txt|log|json|ya?ml|csv|tsv|ini|conf|cfg|env|toml|xml|html?|css|js|ts|py|sh|sql)$/i;
  const TRAILING = /[.,;:!?)}\]】。，；：！？]+$/;

  // 至少两段（/a/b）或者以 ~/ ./ ../ 开头，才算"像路径"：/grid、/OUT-OF-BAND 这种就放过
  // 只屏蔽明确的接口/静态资源前缀；`/home/...` 是 Linux 家目录，不能连坐（曾经把整条路径吃掉）
  const WEB_PREFIX = /^\/(api|assets|static|vendor|mcp|share-assets)\//;
  function plausible(path) {
    if (WEB_PREFIX.test(path)) return false;          // 接口/静态资源不是文件
    if (/^(~|\.{1,2})\//.test(path)) return true;
    return path.split('/').filter(Boolean).length >= 2;
  }

  function cleanPath(raw) {
    let path = String(raw || '').replace(TRAILING, '');
    if (path.length < 2) return '';
    if (!/[A-Za-z0-9_@%+-]/.test(path)) return '';
    return path;
  }

  function statUrl(pane, path) {
    return `/api/panes/${encodeURIComponent(pane)}/files/stat?path=${encodeURIComponent(path)}`;
  }

  function rawHref(pane, path) {
    return `/api/panes/${encodeURIComponent(pane)}/files/raw?path=${encodeURIComponent(path)}`;
  }

  async function lookup(pane, path) {
    const response = await fetch(statUrl(pane, path), {headers: {'Accept': 'application/json'}});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  function wrapTextNode(node, pane, options, cwd) {
    const text = node.textContent || '';
    let match;
    let last = 0;
    const bits = [];
    // 绝对路径优先，其次相对路径（同一个位置只包一次）
    const hits = [];
    for (const [pattern, group] of [[ABSOLUTE, 2], [RELATIVE, 2]]) {
      pattern.lastIndex = 0;
      let found;
      while ((found = pattern.exec(text))) {
        const start = found.index + found[1].length;
        const raw = cleanPath(found[group]);
        if (!raw || !plausible(raw)) continue;
        if (/:\/\//.test(text.slice(Math.max(0, start - 8), start))) continue;   // 跳过 URL
        if (hits.some(hit => start >= hit.start && start < hit.start + hit.raw.length)) continue;
        hits.push({start, raw});
      }
    }
    hits.sort((a, b) => a.start - b.start);
    for (const {start, raw} of hits) {
      if (start < last) continue;
      if (start > last) bits.push(document.createTextNode(text.slice(last, start)));
      const link = document.createElement('a');
      link.className = 'plink';
      link.href = '#';
      link.dataset.plinkPath = raw;
      link.title = raw;
      link.textContent = raw;
      link.__plinkOpen = () => {
        if (link.__plinkAt && Date.now() - link.__plinkAt < 600) return;   // 同一个手势只开一次
        link.__plinkAt = Date.now();
        open(raw, pane, options, cwd);
      };
      link.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        link.__plinkOpen();
      });
      // 鼠标悬停 380ms 弹小卡片：文件名 + 大小 + 预览/下载（触摸不弹，免得一点就冒出来）
      link.addEventListener('pointerenter', event => {
        if (event.pointerType && event.pointerType !== 'mouse') return;
        clearTimeout(hideTimer);
        clearTimeout(cardTimer);
        cardTimer = setTimeout(() => { showCard(link, pane, options, cwd); }, 380);
      });
      link.addEventListener('pointerleave', scheduleHide);
      link.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          link.__plinkOpen();
        }
      });
      bits.push(link);
      last = start + raw.length;
    }
    if (!bits.length) return false;
    if (last < text.length) bits.push(document.createTextNode(text.slice(last)));
    const fragment = document.createDocumentFragment();
    for (const bit of bits) fragment.appendChild(bit);
    node.parentNode.replaceChild(fragment, node);
    return true;
  }

  async function open(path, pane, options, cwd) {
    // `~/x` 交给服务端 expanduser；只有真正的相对路径才拼 pane 的 cwd
    const absolute = path.startsWith('/') || path.startsWith('~/');
    if (!pane) { if (options.onMissing) options.onMissing(path); return; }
    const query = absolute ? path : `${(cwd || '').replace(/\/+$/, '')}/${path}`;
    let info = null;
    try {
      info = await lookup(pane, query);
    } catch (error) {
      if (options.onError) options.onError(error.message);
      return;
    }
    const target = info.path || path;
    if (!info.exists) { if (options.onMissing) options.onMissing(target); return; }
    if (info.kind === 'dir') { if (options.onDir) options.onDir(target); return; }
    if (TEXT_EXT.test(target)) {
      if (options.onPreview) { options.onPreview(target); return; }
      if (MD_EXT.test(target) && options.onMd) { options.onMd(target); return; }
    }
    if (options.onFile) options.onFile(target);
  }

  // ---------------------------------------------------------------- 悬停气泡
  let card = null;
  let cardTimer = 0;
  let hideTimer = 0;
  const statCache = new Map();

  function downloadHref(pane, path) {
    return `/api/panes/${encodeURIComponent(pane)}/files/download?path=${encodeURIComponent(path)}`;
  }

  function ensureCard() {
    if (card) return card;
    card = document.createElement('div');
    card.className = 'plink-card';
    card.hidden = true;
    card.innerHTML = '<div class="plink-card-name"></div><div class="plink-card-meta"></div>'
      + '<div class="plink-card-actions">'
      + '<button type="button" class="plink-card-btn" data-plink-act="preview">预览</button>'
      + '<button type="button" class="plink-card-btn primary" data-plink-act="download">下载</button>'
      + '</div>';
    card.addEventListener('pointerenter', () => clearTimeout(hideTimer));
    card.addEventListener('pointerleave', scheduleHide);
    card.addEventListener('click', event => {
      const button = event.target.closest('[data-plink-act]');
      if (!button || !card.__target) return;
      event.preventDefault();
      event.stopPropagation();
      const {path, pane, options, href, exists, previewable} = card.__target;
      if (button.dataset.plinkAct === 'download') {
        const href = downloadHref(pane, path);
        const name = path.split('/').filter(Boolean).pop() || '文件';
        if (window.TmuxDownload) window.TmuxDownload.maybe(href, {name});
        else { const link = document.createElement('a'); link.href = href; link.download = ''; document.body.appendChild(link); link.click(); link.remove(); }
      } else if (exists) {
        if (previewable && options.onPreview) options.onPreview(path);
        else if (options.onMd && /\.(md|markdown|mdx)$/i.test(path)) options.onMd(path);
        else if (options.onFile) options.onFile(path);
      }
      hideCard();
      void href;
    });
    document.body.appendChild(card);
    return card;
  }

  function hideCard() {
    clearTimeout(cardTimer);
    if (card) card.hidden = true;
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideCard, 180);
  }

  function placeCard(link) {
    const box = link.getBoundingClientRect();
    const panel = ensureCard();
    panel.style.left = '0px';
    panel.style.top = '0px';
    const width = panel.offsetWidth || 220;
    const height = panel.offsetHeight || 86;
    let left = box.left;
    if (left + width > window.innerWidth - 8) left = Math.max(8, window.innerWidth - width - 8);
    let top = box.bottom + 6;
    if (top + height > window.innerHeight - 8) top = Math.max(8, box.top - height - 6);
    panel.style.left = `${Math.round(left)}px`;
    panel.style.top = `${Math.round(top)}px`;
    panel.hidden = false;
  }

  async function showCard(link, pane, options, cwd) {
    const path = link.dataset.plinkPath;
    if (!path || !pane) return;
    const absolute = path.startsWith('/') || path.startsWith('~/');
    const query = absolute ? path : `${(cwd || '').replace(/\/+$/, '')}/${path}`;
    const key = `${pane}|${query}`;
    let info = statCache.get(key);
    if (!info) {
      try {
        info = await lookup(pane, query);
        statCache.set(key, info);
      } catch (_error) {
        return;
      }
    }
    if (card && !card.hidden && card.__link !== link) return;
    const panel = ensureCard();
    panel.__link = link;
    panel.__target = {path: info.path || query, pane, options, exists: info.exists,
      previewable: TEXT_EXT.test(info.path || query)};
    panel.querySelector('.plink-card-name').textContent = info.name || path;
    panel.querySelector('.plink-card-meta').textContent = !info.exists ? '找不到这个路径'
      : info.kind === 'dir' ? '目录'
      : `${info.kind === 'link' ? '符号链接 · ' : ''}${human(info.size)}`;
    const preview = panel.querySelector('[data-plink-act="preview"]');
    if (preview) {
      const canPreview = info.exists && info.kind !== 'dir' && TEXT_EXT.test(info.path || query);
      preview.hidden = !canPreview;
      preview.textContent = canPreview ? '预览' : '打开';
      if (info.exists && info.kind === 'dir') { preview.hidden = false; preview.textContent = '打开目录'; }
    }
    placeCard(link);
  }

  function human(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  function enhance(root, options) {
    if (!root || !options || !options.pane) return 0;
    const pane = options.pane;
    const cwd = options.cwd || '';
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || node.nodeValue.length < 2) return NodeFilter.FILTER_REJECT;
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (SKIP_TAGS.has(parent.tagName)) return NodeFilter.FILTER_REJECT;
        if (parent.closest('[data-plink], a, button')) return NodeFilter.FILTER_REJECT;
        if (parent.closest('pre')) return NodeFilter.FILTER_REJECT;   // 代码块里不动
        if (!node.nodeValue.includes('/')) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const targets = [];
    let node = walker.nextNode();
    while (node) {
      targets.push(node);
      node = walker.nextNode();
    }
    let count = 0;
    for (const textNode of targets) {
      if (wrapTextNode(textNode, pane, options, cwd)) count += 1;
    }
    return count;
  }

  // 手指按下时刚补齐的链接：直接用同一个手势打开（否则要再点一次）
  function activateAt(x, y, scope) {
    // elementFromPoint 有时落在外层容器上（内联链接跨行/两侧留白），所以先在作用域里按矩形找
    let link = null;
    for (const candidate of (scope || document).querySelectorAll('.plink')) {
      const box = candidate.getBoundingClientRect();
      if (x >= box.left && x <= box.right && y >= box.top && y <= box.bottom) { link = candidate; break; }
    }
    if (!link) {
      const element = document.elementFromPoint(x, y);
      link = element && element.closest ? element.closest('.plink') : null;
    }
    if (!link || !link.__plinkOpen) return false;
    link.__plinkOpen();      // 它自己会记时间戳，紧接着的原生 click 会被去重挡掉
    return true;
  }

  window.TmuxPaths = {enhance, rawHref, lookup, activateAt, downloadHref, hideCard};
})();
