/* Search the rendered conversation without inserting elements into its messages. */
(() => {
  const $ = id => document.getElementById(id);
  const transcript = () => $('transcript-messages');
  const registry = window.CSS?.highlights;
  const STRIDE = 128, PAINT_LIMIT = 96;
  let open = false, query = '', total = 0, current = -1;
  let entries = [], cache = new WeakMap(), pattern, activeRange, fallback;
  let observer, scanTimer, inputTimer, refreshTimer;

  function clearPaint() {
    registry?.delete('cs-hit');
    registry?.delete('cs-current');
    fallback?.classList.remove('cs-fallback');
    fallback = activeRange = null;
  }

  function counter(label) {
    const count = $('cs-count');
    count.textContent = label ?? (total ? `${current + 1} / ${total}` : query ? '没有命中' : '');
    count.classList.toggle('bad', !label && !!query && !total);
  }

  // Keep one offset per STRIDE matches; even a common one-character query
  // only creates live DOM Ranges for a small window around the current hit.
  function locate(index) {
    let lo = 0, hi = entries.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (entries[mid].end <= index) lo = mid + 1;
      else hi = mid;
    }
    const {node, offsets, end, count} = entries[lo];
    const ordinal = index - (end - count);
    pattern.lastIndex = offsets[Math.floor(ordinal / STRIDE)];
    let match;
    for (let i = ordinal % STRIDE; i >= 0; i--) {
      match = pattern.exec(node.data);
      if (!match) return null;
    }
    const range = document.createRange();
    range.setStart(node, match.index);
    range.setEnd(node, pattern.lastIndex);
    return range;
  }

  function paint(scroll) {
    clearPaint();
    counter();
    if (current < 0) return;
    activeRange = locate(current);
    if (!activeRange?.startContainer.isConnected) return;
    if (registry && window.Highlight) {
      const hits = new Highlight();
      const start = Math.max(0, Math.min(total - PAINT_LIMIT, current - PAINT_LIMIT / 2));
      for (let i = start; i < Math.min(total, start + PAINT_LIMIT); i++) {
        if (i === current) continue;
        const range = locate(i);
        if (range) hits.add(range);
      }
      registry.set('cs-hit', hits);
      registry.set('cs-current', new Highlight(activeRange));
    } else {
      fallback = activeRange.startContainer.parentElement;
      fallback?.classList.add('cs-fallback');
    }
    if (scroll) {
      const view = $('scroll'), box = activeRange.getBoundingClientRect();
      view.scrollTop += box.top + box.height / 2 - view.getBoundingClientRect().top - view.clientHeight / 2;
    }
  }

  function scan(scroll) {
    clearTimeout(scanTimer);
    const root = transcript();
    if (!root || !open || !query) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const result = [];
    // The scan spans tasks; navigation uses a separate regex cursor.
    const matcher = new RegExp(pattern.source, pattern.flags);
    let node = null, entry, sum = 0;
    function record() {
      if (entry.count) {
        sum += entry.count;
        result.push({...entry, node, end:sum});
      }
      node = null;
    }
    function slice() {
      const until = performance.now() + 6;
      do {
        if (!node) {
          node = walker.nextNode();
          if (!node) {
            const previous = activeRange?.startContainer;
            const previousOffset = activeRange?.startOffset;
            entries = result;
            total = sum;
            current = sum ? Math.min(Math.max(0, current), sum - 1) : -1;
            const same = previous && entries.find(e => e.node === previous);
            if (same) {
              let block = same.offsets.findIndex(offset => offset > previousOffset);
              block = (block < 0 ? same.offsets.length : block) - 1;
              if (block >= 0) {
                matcher.lastIndex = same.offsets[block];
                let n = block * STRIDE, hit;
                while ((hit = matcher.exec(previous.data)) && hit.index < previousOffset) n++;
                if (hit?.index === previousOffset) current = same.end - same.count + n;
              }
            }
            paint(scroll);
            return;
          }
          if (node.parentElement.closest('button,textarea,select,script,style,[hidden]')) { node = null; continue; }
          entry = cache.get(node);
          if (entry && entry.text === node.data) {
            record();
            continue;
          }
          entry = {text:node.data, count:0, offsets:[]};
          matcher.lastIndex = 0;
        }
        for (let i = 0; i < STRIDE; i++) {
          const match = matcher.exec(entry.text);
          if (!match) {
            cache.set(node, entry);
            record();
            break;
          }
          if (entry.count % STRIDE === 0) entry.offsets.push(match.index);
          entry.count++;
        }
      } while (performance.now() < until);
      scanTimer = setTimeout(slice, 0);
    }
    slice();
  }

  function stop() {
    clearTimeout(scanTimer);
    clearTimeout(refreshTimer);
    clearTimeout(inputTimer);
    observer?.disconnect();
    observer = null;
    entries = [];
    cache = new WeakMap();
    total = 0;
    current = -1;
    clearPaint();
  }

  function search(value) {
    stop();
    query = value.trim();
    counter(query ? '查找中…' : '');
    if (!open || !query || !transcript()) return;
    pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu');
    observer = new MutationObserver(() => {
      clearTimeout(scanTimer);
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => scan(false), 100);
    });
    observer.observe(transcript(), {childList:true, characterData:true, subtree:true});
    scan(true);
  }

  function step(delta) {
    if (!total) return;
    current = (current + delta + total) % total;
    paint(true);
  }

  function setOpen(next) {
    open = next;
    $('chat-search').hidden = !next;
    $('chat-search-button')?.setAttribute('aria-pressed', String(next));
    const input = $('cs-input');
    if (next) {
      input.focus({preventScroll:true});
      input.select();
    } else {
      stop();
      query = input.value = '';
      counter();
    }
  }

  function mount() {
    const button = $('chat-search-button'), input = $('cs-input');
    if (!button || button.dataset.bound) return;
    button.dataset.bound = '1';
    button.addEventListener('click', () => setOpen(!open));
    input.addEventListener('input', () => {
      clearTimeout(inputTimer);
      inputTimer = setTimeout(() => search(input.value), 120);
    });
    input.addEventListener('keydown', event => {
      if (event.isComposing) return;
      if (event.key === 'Enter') { event.preventDefault(); step(event.shiftKey ? -1 : 1); }
      if (event.key === 'Escape') { event.preventDefault(); setOpen(false); button.focus(); }
    });
    $('cs-prev').onclick = () => step(-1);
    $('cs-next').onclick = () => step(1);
    $('cs-close').onclick = () => { setOpen(false); button.focus(); };
    document.addEventListener('keydown', event => {
      if (button.disabled || !(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'f') return;
      if (document.activeElement?.matches('input,textarea') || document.activeElement?.isContentEditable) return;
      event.preventDefault();
      setOpen(true);
    }, true);
  }
  window.TmuxChatSearch = {
    mount, open:() => setOpen(true), close:() => setOpen(false), toggle:() => setOpen(!open),
    search(value) { setOpen(true); $('cs-input').value = value || ''; search($('cs-input').value); },
    next:() => step(1), prev:() => step(-1), state:() => ({open, query, hits:total, current}),
  };
})();
