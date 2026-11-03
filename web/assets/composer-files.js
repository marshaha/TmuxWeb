/* Attachments for the chat composer — one implementation shared by the console
   (/index.html) and the embedded /preview used inside multi-window cells.
 *
 * Files are uploaded into  <pane cwd>/upload_files/  and the outgoing message gets a
 * leading line:  user uploaded file “a.png & b.pdf”
 *
 * Usage:
 *   const files = TmuxFiles.mount({
 *     strip, button, picker,               // elements
 *     pane:  () => currentPaneId(),        // string
 *     path:  () => currentPanePath(),      // used to build <path>/upload_files
 *     onError: message => …,
 *     onBusy:  busy => …,                  // optional: disable send while uploading
 *   });
 *   files.prompt()   -> the header line (or '')
 *   files.clear()    -> drop the strip (files stay on disk)
 */
(function () {
  const LIMIT = 256 * 1024 * 1024;

  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g,
    ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));

  function humanSize(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  function mount(options) {
    const strip = options.strip;
    const button = options.button;
    const picker = options.picker;
    const items = [];
    const isImage = file => /^image\//.test((file && file.type) || '');

    const paneId = () => (options.pane ? options.pane() : '');
    function directory() {
      if (options.dir) return options.dir();
      const base = String((options.path && options.path()) || '').replace(/\/+$/, '');
      return base ? base + '/upload_files' : '';
    }
    function endpoint(name, action) {
      const pane = paneId();
      const dir = directory();
      if (action === 'download') {
        return `/api/panes/${encodeURIComponent(pane)}/files/download?path=${encodeURIComponent(dir + '/' + name)}`;
      }
      return `/api/panes/${encodeURIComponent(pane)}/files/upload`
        + `?name=${encodeURIComponent(name)}&dir=${encodeURIComponent(dir)}`;
    }

    function notifyBusy() {
      if (options.onBusy) options.onBusy(items.some(item => item.pending));
    }

    function render() {
      if (!strip) return;
      strip.hidden = !items.length;
      strip.innerHTML = items.map((item, index) => {
        const remove = `<button class="cf-x" type="button" data-cf-drop="${index}" title="移除" aria-label="移除">`
          + `<svg class="ico"><use href="#i-close"/></svg></button>`;
        if (item.pending) {
          return `<div class="cf-item busy"><svg class="ico"><use href="#i-paperclip"/></svg>`
            + `<span class="cf-meta"><span class="cf-name">${esc(item.name)}</span>`
            + `<span class="cf-size">上传中…</span></span>${remove}</div>`;
        }
        if (item.isImage) {
          return `<div class="cf-item shot" title="${esc(item.name)}">`
            + `<img src="${esc(item.url)}" alt="${esc(item.name)}">${remove}</div>`;
        }
        return `<div class="cf-item"><svg class="ico"><use href="#i-file"/></svg>`
          + `<span class="cf-meta"><span class="cf-name">${esc(item.name)}</span>`
          + `<span class="cf-size">${esc(humanSize(item.size))}</span></span>${remove}</div>`;
      }).join('');
      notifyBusy();
    }

    function uniqueName(name) {
      const taken = new Set(items.map(item => item.name));
      if (!taken.has(name)) return name;
      const dot = name.lastIndexOf('.');
      const stem = dot > 0 ? name.slice(0, dot) : name;
      const ext = dot > 0 ? name.slice(dot) : '';
      for (let i = 2; i < 1000; i += 1) {
        const candidate = `${stem}-${i}${ext}`;
        if (!taken.has(candidate)) return candidate;
      }
      return `${stem}-${Date.now()}${ext}`;
    }

    async function add(files) {
      const list = [...(files || [])];
      if (!list.length) return;
      if (!paneId()) { if (options.onError) options.onError('先选择一个会话再上传附件'); return; }
      if (!directory()) { if (options.onError) options.onError('这个会话没有可用目录，无法上传附件'); return; }
      for (const file of list) {
        if (file.size > LIMIT) { if (options.onError) options.onError(`${file.name} 超过 ${humanSize(LIMIT)}`); continue; }
        const name = uniqueName(file.name || 'file');
        const item = {name, size: file.size, isImage: isImage(file), pending: true,
          objectUrl: isImage(file) ? URL.createObjectURL(file) : null};
        items.push(item);
        render();
        try {
          const response = await fetch(endpoint(name, 'upload'), {method: 'PUT', body: file});
          if (!response.ok) {
            let detail = '';
            try { detail = (await response.json()).detail || ''; } catch (_error) { /* plain body */ }
            throw new Error(detail || `HTTP ${response.status}`);
          }
          item.pending = false;
          item.url = endpoint(name, 'download');
          item.onDisk = true;
        } catch (error) {
          const at = items.indexOf(item);
          if (at >= 0) items.splice(at, 1);
          if (item.objectUrl) URL.revokeObjectURL(item.objectUrl);
          if (options.onError) options.onError(`附件上传失败：${error.message}`);
        }
        render();
      }
    }

    async function remove(index) {
      const item = items[index];
      if (!item) return;
      items.splice(index, 1);
      render();
      if (item.objectUrl) URL.revokeObjectURL(item.objectUrl);
      if (item.onDisk && paneId()) {
        fetch(endpoint(item.name, 'upload'), {method: 'DELETE'}).catch(() => {});
      }
    }

    function clear() {
      for (const item of items) if (item.objectUrl) URL.revokeObjectURL(item.objectUrl);
      items.length = 0;
      render();
    }

    function prompt() {
      const names = items.filter(item => !item.pending).map(item => item.name);
      return names.length ? `user uploaded file “${names.join(' & ')}”` : '';
    }

    function syncButton() {
      if (button) button.disabled = !paneId();
    }

    if (button && picker) {
      button.addEventListener('click', () => {
        if (!paneId()) return;
        picker.click();
      });
    }
    if (picker) {
      picker.addEventListener('change', event => {
        const chosen = [...(event.target.files || [])];
        event.target.value = '';
        add(chosen);
      });
    }
    if (strip) {
      strip.addEventListener('click', event => {
        const hit = event.target.closest('[data-cf-drop]');
        if (!hit) return;
        event.preventDefault();
        remove(Number(hit.dataset.cfDrop));
      });
    }
    const drop = options.drop;
    if (drop) {
      drop.addEventListener('dragover', event => {
        if (!paneId()) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
      });
      drop.addEventListener('drop', event => {
        if (!paneId()) return;
        event.preventDefault();
        add([...(event.dataTransfer.files || [])]);
      });
    }

    syncButton();
    render();
    return {add, remove, clear, prompt, render, syncButton,
      count: () => items.length,
      pending: () => items.some(item => item.pending)};
  }

  window.TmuxFiles = {mount};
})();
