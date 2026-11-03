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
  const ABSOLUTE = /(^|[\s(（\[【<"'`])((?:~|\.{1,2})?\/[^\s'"`<>|，。；：、）)】]+)/g;
  const RELATIVE = /(^|[\s(（\[【<"'`])((?:\.{1,2}\/)?(?:[\w@%+.-]+\/)+[\w@%+.-]+\.[A-Za-z0-9]{1,8})/g;
  const SKIP_TAGS = new Set(['A', 'PRE', 'SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'BUTTON', 'SVG']);
  const MD_EXT = /\.(md|markdown|mdx)$/i;
  const TRAILING = /[.,;:!?)}\]】。，；：！？]+$/;

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
        if (!raw) continue;
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
      link.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        open(raw, pane, options, cwd);
      });
      link.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          open(raw, pane, options, cwd);
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
    if (MD_EXT.test(target) && options.onMd) { options.onMd(target); return; }
    if (options.onFile) options.onFile(target);
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
        if (!/[~./]\//.test(node.nodeValue)) return NodeFilter.FILTER_REJECT;
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

  window.TmuxPaths = {enhance, rawHref, lookup};
})();
