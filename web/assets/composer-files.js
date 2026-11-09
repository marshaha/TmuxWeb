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

  // 缩略图在本地生成：直接解码原图就是原尺寸，一张 5MB 的截图在手机上会占掉几十 MB，
  // 而且解码那一下界面会顿。createImageBitmap 解码、canvas 缩到 320px，再转成
  // 小的 jpeg blob —— 一个文件几毫秒，几个文件并行跑完立刻能显示。
  const THUMB_EDGE = 320;
  async function thumbnail(file) {
    try {
      const bitmap = await createImageBitmap(file);
      const scale = Math.min(1, THUMB_EDGE / Math.max(bitmap.width, bitmap.height));
      const width = Math.max(1, Math.round(bitmap.width * scale));
      const height = Math.max(1, Math.round(bitmap.height * scale));
      const canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext('2d');
      context.drawImage(bitmap, 0, 0, width, height);
      if (bitmap.close) bitmap.close();
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.82));
      return blob ? {url: URL.createObjectURL(blob), blob} : null;
    } catch (error) {
      return null;      // 认不出的格式（heic 之类）就退回用原图地址
    }
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
          const ratio = Math.max(0, Math.min(1, item.progress || 0));
          // 大文件在几十 KB/s 的链路上，一分钟也可能不到 1%：整数会一直显示 0%，
          // 所以小于 10% 时保留一位小数，并把已传字节一起写出来。
          const pct = ratio >= 0.1 ? String(Math.round(ratio * 100)) : (ratio * 100).toFixed(1);
          const sent = item.sent ? humanSize(item.sent) : '';
          return `<div class="cf-item busy"><svg class="ico"><use href="#i-paperclip"/></svg>`
            + `<span class="cf-meta"><span class="cf-name">${esc(item.name)}</span>`
            + `<span class="cf-size">上传中 ${pct}%${sent ? ` · ${sent} / ${humanSize(item.size)}` : ''}</span></span>${remove}</div>`;
        }
        if (item.failed) {
          return `<div class="cf-item failed" data-cf-retry="${index}"><svg class="ico"><use href="#i-refresh"/></svg>`
            + `<span class="cf-meta"><span class="cf-name">${esc(item.name)}</span>`
            + `<span class="cf-size">上传失败 · 点这里重试</span></span>${remove}</div>`;
        }
        if (item.isImage) {
          const source = item.thumb || item.url;
          const picture = source
            ? `<img src="${esc(source)}" alt="${esc(item.name)}">`
            : `<span class="cf-shot-wait"></span>`;      // 缩略图还没生成好
          return `<div class="cf-item shot" title="${esc(item.name)}">${picture}${remove}</div>`;
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
      const accepted = [];
      for (const file of list) {
        if (file.size > LIMIT) { if (options.onError) options.onError(`${file.name} 超过 ${humanSize(LIMIT)}`); continue; }
        const name = uniqueName(file.name || 'file');
        const item = {name, size: file.size, isImage: isImage(file), pending: true,
          thumb: null, thumbFailed: false, file};
        items.push(item);
        accepted.push(item);
      }
      render();      // 先把占位放上去（多张图时不用等任何一张）
      // 缩略图并行生成：几张截图几乎同时出现
      accepted.filter(item => item.isImage).forEach(item => {
        thumbnail(item.file).then(result => {
          item.thumb = result ? result.url : null;
          item.thumbBlob = result ? result.blob : null;
          item.thumbFailed = !result;
          render();
        });
      });
      for (const item of accepted) await push(item);
    }

    async function push(item) {
      item.pending = true;
      item.failed = false;
      item.progress = 0;
      render();
      try {
        if (!window.TmuxUpload) throw new Error('上传模块未加载');
        await window.TmuxUpload.upload({
          pane: paneId(), dir: directory(), name: item.name, file: item.file,
          onProgress: (done, total) => {
            item.progress = total ? done / total : 0;
            item.sent = done;
            item.size = total;
            render();
          },
        });
        item.pending = false;
        item.url = endpoint(item.name, 'download');
        item.onDisk = true;
        pushThumb(item);
      } catch (error) {
        // 失败不再静默删掉：留一条"点这里重试"，已经上传的块在服务端 .part 里
        item.pending = false;
        item.failed = true;
        if (options.onError) options.onError(`附件上传失败：${error.message}`);
      }
      render();
    }

    // 聊天气泡只用这张 320px 的：原图解码太贵，十几张截图就能把浏览器拖死。
    // 失败无所谓，服务端拿不到 sidecar 就回落到原图。
    async function pushThumb(item) {
      if (!item.thumbBlob || !window.TmuxUpload || !paneId()) return;
      try {
        await window.TmuxUpload.upload({
          pane: paneId(), dir: directory() + '/.thumbs',
          name: item.name.replace(/\.[^.]+$/, '') + '.jpg',
          file: new File([item.thumbBlob], 'thumb.jpg', {type: 'image/jpeg'}),
        });
      } catch (error) { /* 只是优化，失败不影响发送 */ }
    }

    async function remove(index) {
      const item = items[index];
      if (!item) return;
      items.splice(index, 1);
      render();
      if (item.thumb) URL.revokeObjectURL(item.thumb);
      if (item.onDisk && paneId()) {
        fetch(endpoint(item.name, 'upload'), {method: 'DELETE'}).catch(() => {});
      }
    }

    function clear() {
      for (const item of items) if (item.thumb) URL.revokeObjectURL(item.thumb);
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
        const drop = event.target.closest('[data-cf-drop]');
        if (drop) {
          event.preventDefault();
          remove(Number(drop.dataset.cfDrop));
          return;
        }
        const retry = event.target.closest('[data-cf-retry]');
        if (retry) {
          event.preventDefault();
          const item = items[Number(retry.dataset.cfRetry)];
          if (item && item.failed) push(item);
        }
      });
    }
    // ---- 粘贴板图片：Ctrl/Cmd+V 直接变成附件 -------------------------------
    // 截图工具（微信、macOS 截图、手机长按复制）给的是一张没有文件名的 image/png，
    // 直接塞进同一条 add() 里它就走上传、进度、缩略图那套，不用再点「附件」。
    const GENERIC_PASTE_NAME = /^(image|blob|clipboard|untitled)\.[a-z0-9]{2,5}$|^blob$/i;
    const PASTE_EXT = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif',
                       'image/webp': 'webp', 'image/bmp': 'bmp', 'image/svg+xml': 'svg'};
    function pastedName(type) {
      const ext = PASTE_EXT[type] || 'png';
      const now = new Date();
      const pad = value => String(value).padStart(2, '0');
      const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
        + `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
      return `pasted-${stamp}.${ext}`;
    }
    function clipboardImages(event) {
      const data = event.clipboardData;
      if (!data) return [];
      const found = [];
      const items = data.items || [];
      for (const entry of items) {
        if (entry.kind !== 'file') continue;
        const type = entry.type || '';
        if (!/^image\//.test(type)) continue;
        const blob = entry.getAsFile();
        if (!blob) continue;
        // 剪贴板里的图片通常叫 image.png / blob，直接用会互相盖掉；只有真名字才保留
        const name = blob.name && !GENERIC_PASTE_NAME.test(blob.name) && /\.[a-z0-9]{2,5}$/i.test(blob.name)
          ? blob.name : pastedName(type);
        found.push(new File([blob], name, {type: blob.type || type}));
      }
      if (!found.length && data.files) {
        for (const file of data.files) {
          if (/^image\//.test(file.type || '')) {
            found.push(file.name ? file : new File([file], pastedName(file.type), {type: file.type}));
          }
        }
      }
      return found;
    }
    const pasteTarget = options.drop || document.body;
    if (pasteTarget) {
      pasteTarget.addEventListener('paste', event => {
        const images = clipboardImages(event);
        if (!images.length) return;      // 纯文字粘贴：交给浏览器默认行为
        event.preventDefault();
        add(images);
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

  // 命名规则单独露出来，测试才能盯住"剪贴板里的 image.png 不能直接落盘成 image.png"
  window.TmuxFiles = {mount, pastedNameFor: (type, originalName) => {
    const generic = /^(image|blob|clipboard|untitled)\.[a-z0-9]{2,5}$|^blob$/i;
    if (originalName && !generic.test(originalName) && /\.[a-z0-9]{2,5}$/i.test(originalName)) {
      return originalName;
    }
    const ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp',
                 'image/bmp': 'bmp', 'image/svg+xml': 'svg'}[type] || 'png';
    const now = new Date();
    const pad = value => String(value).padStart(2, '0');
    return `pasted-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
      + `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.${ext}`;
  }};
})();
