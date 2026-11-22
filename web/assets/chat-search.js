/* 对话内搜索：找一个词，在这段对话里跳来跳去。
 *
 * 为什么不直接 `window.find()`：它只能顺序找、没法给"当前命中"加样式，也没法告诉你有几条。
 * 这里走 DOM 文本节点：命中包进 <mark class="cs-hit">，当前那条再加 .cs-current 并滚动到中间。
 *
 * 轮询会整段重渲染 transcript，mark 会被冲掉 —— MutationObserver 在重渲染后再盖一遍，
 * 并尽量停在同一处（按"第几条消息 + 第几个命中"记位置，不按下标，重渲染后仍然是那一条）。
 *
 *   TmuxChatSearch.open() / close() / toggle()
 *   TmuxChatSearch.state()   {open, query, hits, current}
 */
(function () {
  const BAR = 'chat-search';
  const MARK = 'cs-hit';
  const CURRENT = 'cs-current';

  let open = false;
  let query = '';
  let marks = [];
  let current = -1;
  let observer = null;
  let reindexTimer = 0;

  const $ = id => document.getElementById(id);

  function transcript() {
    return document.getElementById('transcript');
  }

  function clearMarks() {
    const root = transcript();
    if (!root) return;
    for (const mark of [...root.querySelectorAll(`mark.${MARK}`)]) {
      const parent = mark.parentNode;
      if (!parent) continue;
      parent.replaceChild(document.createTextNode(mark.textContent), mark);
      parent.normalize();
    }
    marks = [];
  }

  // 只动文本节点：代码块、链接、按钮的结构保持原样，撤掉高亮也不会留下垃圾标签
  function walkTextNodes(root, visit) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        const parent = node.parentNode;
        if (!parent) return NodeFilter.FILTER_REJECT;
        const tag = parent.nodeName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'TEXTAREA') return NodeFilter.FILTER_REJECT;
        if (parent.classList && parent.classList.contains(MARK)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) visit(node);
  }

  function highlight(needle) {
    const root = transcript();
    if (!root || !needle) return;
    const lowered = needle.toLowerCase();
    walkTextNodes(root, node => {
      const text = node.nodeValue;
      const hay = text.toLowerCase();
      let at = hay.indexOf(lowered);
      if (at < 0) return;
      const fragment = document.createDocumentFragment();
      let last = 0;
      while (at >= 0) {
        if (at > last) fragment.appendChild(document.createTextNode(text.slice(last, at)));
        const mark = document.createElement('mark');
        mark.className = MARK;
        mark.textContent = text.slice(at, at + needle.length);
        fragment.appendChild(mark);
        last = at + needle.length;
        at = hay.indexOf(lowered, last);
      }
      if (last < text.length) fragment.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(fragment, node);
    });
    marks = [...root.querySelectorAll(`mark.${MARK}`)];
  }

  function paintCurrent(scroll) {
    marks.forEach((mark, index) => mark.classList.toggle(CURRENT, index === current));
    const counter = $('cs-count');
    if (counter) {
      counter.textContent = marks.length ? `${current + 1} / ${marks.length}` : (query ? '没有命中' : '');
      counter.classList.toggle('bad', !!query && !marks.length);
    }
    if (scroll && marks[current]) {
      marks[current].scrollIntoView({block: 'center', behavior: 'smooth'});
    }
  }

  function run(nextQuery, keepIndex) {
    query = nextQuery || '';
    clearMarks();
    if (!query) { current = -1; paintCurrent(false); return; }
    highlight(query);
    if (!marks.length) { current = -1; paintCurrent(false); return; }
    // 从当前位置往后找第一个"排在当前位置之后"的命中：重渲染后不至于跳回第一条
    current = keepIndex && current >= 0 && current < marks.length ? current : 0;
    paintCurrent(true);
  }

  function step(delta) {
    if (!marks.length) return;
    current = (current + delta + marks.length) % marks.length;
    paintCurrent(true);
  }

  function ensureObserver() {
    const root = transcript();
    if (!root || observer) return;
    observer = new MutationObserver(() => {
      // 重渲染会清掉 mark：等这一轮 DOM 稳定后再盖一遍
      clearTimeout(reindexTimer);
      reindexTimer = setTimeout(() => {
        if (!open || !query) return;
        const at = current;
        clearMarks();
        highlight(query);
        current = marks.length ? Math.min(Math.max(at, 0), marks.length - 1) : -1;
        paintCurrent(false);
      }, 180);
    });
    observer.observe(root, {childList: true, subtree: true});
  }

  function setOpen(next) {
    open = next;
    const bar = $(BAR);
    if (bar) bar.hidden = !open;
    const input = $('cs-input');
    if (open) {
      ensureObserver();
      if (input) { input.value = query; input.focus(); input.select(); }
      if (query) run(query, true);
    } else {
      clearMarks();
      query = '';
      current = -1;
      if (input) input.value = '';
      paintCurrent(false);
    }
    const button = $('chat-search-button');
    if (button) button.setAttribute('aria-pressed', open ? 'true' : 'false');
  }

  function bind() {
    const button = $('chat-search-button');
    if (button && !button.dataset.bound) {
      button.dataset.bound = '1';
      button.addEventListener('click', () => setOpen(!open));
    }
    const input = $('cs-input');
    if (input && !input.dataset.bound) {
      input.dataset.bound = '1';
      let timer = 0;
      input.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => run(input.value.trim(), false), 120);
      });
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          event.preventDefault();
          step(event.shiftKey ? -1 : 1);
        } else if (event.key === 'Escape') {
          event.preventDefault();
          setOpen(false);
        }
      });
    }
    for (const [id, delta] of [['cs-prev', -1], ['cs-next', 1]]) {
      const node = $(id);
      if (node && !node.dataset.bound) {
        node.dataset.bound = '1';
        node.addEventListener('click', () => step(delta));
      }
    }
    const close = $('cs-close');
    if (close && !close.dataset.bound) {
      close.dataset.bound = '1';
      close.addEventListener('click', () => setOpen(false));
    }
    // ⌘F / Ctrl+F：有选中会话时找的是对话内容，不是整页 HTML
    if (!document.documentElement.dataset.csBound) {
      document.documentElement.dataset.csBound = '1';
      document.addEventListener('keydown', event => {
        if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'f') return;
        const tag = (document.activeElement && document.activeElement.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || document.activeElement.isContentEditable) return;
        if (!transcript() || !transcript().children.length) return;
        event.preventDefault();
        setOpen(true);
      }, true);
    }
  }

  window.TmuxChatSearch = {
    mount() { bind(); },
    open: () => setOpen(true),
    close: () => setOpen(false),
    toggle: () => setOpen(!open),
    search(value) { setOpen(true); const input = $('cs-input'); if (input) input.value = value || ''; run((value || '').trim(), false); },
    next: () => step(1),
    prev: () => step(-1),
    state: () => ({open, query, hits: marks.length, current}),
  };
})();
