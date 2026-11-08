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
  const CHUNK = 4 * 1024 * 1024;
  const RETRIES = 3;

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

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
    const notify = () => { if (onProgress) onProgress(offset, total); };
    notify();
    while (offset < total) {
      const end = Math.min(offset + CHUNK, total);
      const blob = file.slice(offset, end);
      let response;
      try {
        response = await fetch(endpoint(pane, name, dir, offset, total, tag), {method: 'PUT', body: blob});
      } catch (error) {
        // 网络层失败（掉线、切后台被挂起）：同一块退避重试，不从头再来
        attempts += 1;
        if (attempts > RETRIES) throw new Error(`上传中断：${error.message || '网络错误'}`);
        await sleep(500 * attempts);
        continue;
      }
      if (response.status === 409) {
        const header = Number(response.headers.get('X-Upload-Offset'));
        let server = header;
        if (!Number.isFinite(server)) {
          try { server = Number((await response.json()).offset); } catch (_error) { server = 0; }
        }
        offset = Number.isFinite(server) && server >= 0 ? server : 0;
        notify();
        continue;                       // 对面已经有的字节比我们想的少/多，从它那里接着传
      }
      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try { detail = (await response.json()).detail || detail; } catch (_error) { /* plain */ }
        if (response.status >= 500) {
          attempts += 1;
          if (attempts <= RETRIES) { await sleep(500 * attempts); continue; }
        }
        throw new Error(detail);
      }
      attempts = 0;
      offset = end;
      notify();
    }
    return {path: `${dir}/${name}`, size: total, token: tag};
  }

  window.TmuxUpload = {upload, CHUNK, token};
})();
