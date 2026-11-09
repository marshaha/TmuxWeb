/* Uploaded files inside a chat bubble.
 *
 * The composer prepends one line to the message:
 *     user uploaded file “a.png & b.pdf”
 * That line is the only trace left in the transcript, and the file name alone
 * does not tell you what the picture was. This module turns the line into a
 * preview grid: images become thumbnails (click opens the full file), anything
 * else stays a named chip. Bytes come from /api/panes/<pane>/uploads/<name>,
 * which looks the file up next to the pane's cwd.
 *
 *   TmuxUploads.bubbleHtml(text, pane)  -> HTML for a user bubble
 *   TmuxUploads.rawUrl(pane, name)      -> the byte URL
 */
(function () {
  const UPLOAD_RE = /user uploaded file\s*[“"]([^”"\n]+?)[”"]/g;
  const IMAGE_RE = /\.(png|jpe?g|gif|webp|bmp|avif|svg)$/i;

  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g,
    ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));

  function rawUrl(pane, name) {
    return `/api/panes/${encodeURIComponent(pane)}/uploads/${encodeURIComponent(name)}`;
  }

  function item(pane, name) {
    const url = rawUrl(pane, name);
    if (IMAGE_RE.test(name)) {
      return `<a class="upload-thumb" data-upload="1" href="${esc(url)}" target="_blank" rel="noopener" title="${esc(name)}">`
        + `<img data-upload="1" src="${esc(url)}" alt="${esc(name)}" loading="lazy" decoding="async"`
        + ` onerror="this.closest('.upload-thumb').classList.add('broken')"></a>`;
    }
    return `<a class="upload-file" data-upload="1" href="${esc(url)}" target="_blank" rel="noopener">`
      + `<svg class="ico"><use href="#i-file"/></svg><span>${esc(name)}</span></a>`;
  }

  function bubbleHtml(text, pane) {
    const body = String(text == null ? '' : text);
    // 没有 pane 就拼不出地址：原样显示，别把文字弄丢
    if (!pane || body.indexOf('user uploaded file') < 0) return esc(body);
    let out = '';
    let last = 0;
    let matched = false;
    UPLOAD_RE.lastIndex = 0;
    let hit = UPLOAD_RE.exec(body);
    while (hit) {
      // 分隔符是 " & "（两边带空格）：文件名里单个 & 很常见（a&b.png），
      // 按裸 & 切会把一个文件拆成两个
      const names = hit[1].split(/\s+&\s+/).map(part => part.trim()).filter(Boolean);
      if (names.length) {
        matched = true;
        const before = body.slice(last, hit.index);
        out += esc(before.replace(/\n+$/, '\n')) + `<div class="upload-grid">`
          + names.map(name => item(pane, name)).join('') + '</div>';
        last = hit.index + hit[0].length;
        if (body.slice(last, last + 1) === '\n') last += 1;   // 那一行的换行一起吃掉
      }
      hit = UPLOAD_RE.exec(body);
    }
    if (!matched) return esc(body);
    out += esc(body.slice(last).replace(/^\n+/, ''));
    return out.trim() ? out : esc(body);
  }

  function isImage(name) {
    return IMAGE_RE.test(String(name || ''));
  }

  window.TmuxUploads = {bubbleHtml, rawUrl, isImage};
})();
