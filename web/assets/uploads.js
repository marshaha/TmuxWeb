/* 分块上传：进度、断点续传、按块重试。
 *
 * 为什么不用一个 PUT 把整个文件发过去：
 *   - 慢链路上（macmini 上行几十 KB/s）一次 30 秒的超时就会让整份文件白传；
 *   - 经过 Cloudflare 的域名单请求上限 100MB，大文件直接 413。
 * 4MB 一块后，失败只丢一块，进度能显示，而单请求永远在 CF 的上限之下。
 *
 * 服务端把块追加到 <dir>/.<token>-<name>.part，最后一块写完才改名成目标文件，
 * 所以别人永远看不到半个文件。返回 409 + X-Upload-Offset 表示"对面已经有多少字节"，
 * 客户端据此对齐后用 GET /files/upload?token=... 也能接着传。
 */
(function () {
  const CHUNK_MAX = 4 * 1024 * 1024;
  const CHUNK_MIN = 256 * 1024;
  const CHUNK = CHUNK_MAX;          // 兼容旧调用方
  const RETRIES = 3;

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  // fetch 没有上传进度事件：4MB 一块时块内进度是看不见的，小文件(单块)更是一直 0%。
  // 用 XHR 的 upload.onprogress 才能拿到"这一块已经发出多少字节"。
  function sendChunk(url, blob, onSent) {
    return new Promise(resolve => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', url, true);
      xhr.responseType = 'text';
      if (xhr.upload && onSent) {
        xhr.upload.onprogress = event => { if (event.loaded) onSent(event.loaded); };
      }
      xhr.onload = () => resolve({ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status,
        header: xhr.getResponseHeader('X-Upload-Offset'), body: xhr.responseText || ''});
      xhr.onerror = () => resolve({networkError: 'network'});
      xhr.ontimeout = () => resolve({networkError: 'timeout'});
      xhr.onabort = () => resolve({networkError: 'aborted'});
      xhr.send(blob);
    });
  }

  function token(name, size) {
    const text = `${name}:${size}`;
    let hash = 0;
    for (let i = 0; i < text.length; i += 1) hash = (hash * 31 + text.charCodeAt(i)) | 0;
    return `up${(hash >>> 0).toString(36)}`;
  }

  function endpoint(pane, name, dir, offset, total, tag) {
    const query = new URLSearchParams({name, dir, offset: String(offset), total: String(total), token: tag});
    return `/api/panes/${encodeURIComponent(pane)}/files/upload?${query.toString()}`;
  }

  async function upload(options) {
    const {pane, dir, name, file, onProgress} = options;
    const total = file.size;
    const tag = options.token || token(name, total);
    let offset = 0;
    let attempts = 0;
    // 慢链路上 4MB 一块要传好几分钟，一旦断掉那一块白传；从 1MB 起步，
    // 快的链路再放大，慢的链路缩小 —— 进度也因此更细。
    let chunkSize = Math.min(CHUNK_MAX, Math.max(CHUNK_MIN, Math.ceil(total / 8) || CHUNK_MIN));
    const notify = () => { if (onProgress) onProgress(offset, total); };
    notify();
    while (offset < total) {
      const end = Math.min(offset + chunkSize, total);
      const blob = file.slice(offset, end);
      const started = performance.now();
      const response = await sendChunk(endpoint(pane, name, dir, offset, total, tag), blob, sent => {
        if (onProgress) onProgress(offset + Math.min(sent, blob.size), total);
      });
      if (response.networkError) {
        // 网络层失败（掉线、切后台被挂起）：同一块退避重试，不从头再来
        attempts += 1;
        if (attempts > RETRIES) throw new Error(`上传中断：${response.networkError}`);
        await sleep(500 * attempts);
        continue;
      }
      if (response.status === 409) {
        let server = Number(response.header);
        if (!Number.isFinite(server)) {
          try { server = Number(JSON.parse(response.body).offset); } catch (_error) { server = 0; }
        }
        offset = Number.isFinite(server) && server >= 0 ? server : 0;
        notify();
        continue;                       // 对面已经有的字节和我们的预期不一致，从它那里接着传
      }
      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try { detail = JSON.parse(response.body).detail || detail; } catch (_error) { /* plain */ }
        if (response.status >= 500) {
          attempts += 1;
          if (attempts <= RETRIES) { await sleep(500 * attempts); continue; }
        }
        throw new Error(detail);
      }
      attempts = 0;
      offset = end;
      const seconds = Math.max(0.001, (performance.now() - started) / 1000);
      const speed = blob.size / seconds;
      if (speed > 2 * 1024 * 1024) chunkSize = Math.min(CHUNK_MAX, chunkSize * 2);
      else if (speed < 200 * 1024) chunkSize = Math.max(CHUNK_MIN, Math.floor(chunkSize / 2));
      notify();
    }
    return {path: `${dir}/${name}`, size: total, token: tag};
  }

  window.TmuxUpload = {upload, CHUNK: CHUNK_MAX, token};
})();
