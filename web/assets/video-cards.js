/* 聊天里的视频：链接（或本地路径）→ 就地播放器。
 *
 * 为什么单独一个文件：index.html 已经很大，而且视频卡片的逻辑要能在浏览器里
 * 单独喂一段 HTML 验证（见 tests/video_cards.test.mjs 的配套手工验证）。
 *
 * 规则：
 *   - 只认 http(s)/blob 链接，别的协议（javascript:、data:）一律不嵌入；
 *   - 认扩展名 mp4/m4v/webm/mov/ogv/ogg/mkv，以及 /files/raw?path=…mp4 这种本地路径；
 *   - preload="metadata"：不点播放只取时长和第一帧，不会把整段视频拉下来；
 *   - 播放器下面保留原始链接，方便另存或换窗口；
 *   - 卡片宽度跟着气泡走，手机上限高，不撑破布局。
 */
(function () {
  const VIDEO_URL_RE = /\.(mp4|m4v|webm|mov|ogv|ogg|mkv)(?:[?#]|$)/i;

  function videoLikeName(url) {
    if (!url) return '';
    if (url.pathname.endsWith('/files/raw')) {
      const inside = url.searchParams.get('path') || '';
      return VIDEO_URL_RE.test(inside) ? inside.split('/').pop() : '';
    }
    let decoded = url.pathname;
    try { decoded = decodeURIComponent(url.pathname); } catch (_error) { /* keep raw */ }
    return VIDEO_URL_RE.test(decoded) ? decoded.split('/').pop() : '';
  }

  // 第三方链接（openlist、对象存储签名链接）常常发 octet-stream + attachment，
  // 浏览器拿到就不肯播；签名又改不了，只能让自家服务端代取一次。
  function playableSrc(url) {
    if (!/^https?:$/.test(url.protocol)) return url.href;
    if (url.origin === location.origin) return url.href;      // 自己的 /api/… 直接播
    return `${location.origin}/api/media/proxy?url=${encodeURIComponent(url.href)}`;
  }

  function card(url, name) {
    const figure = document.createElement('figure');
    figure.className = 'video-card';

    const video = document.createElement('video');
    video.controls = true;
    video.playsInline = true;
    video.preload = 'metadata';
    video.setAttribute('controlsList', 'nodownload');
    // 加一个 #t=0.5 媒体片段：Chrome 会立刻解一帧画出来，否则 metadata 到了也还是黑框
    const base = playableSrc(url);
    video.src = base.includes('#') ? base : `${base}#t=0.5`;
    const fallback = document.createElement('a');
    fallback.href = url.href;
    fallback.target = '_blank';
    fallback.rel = 'noopener noreferrer';
    fallback.textContent = '这个格式浏览器可能放不了，点这里打开';
    video.appendChild(fallback);

    const caption = document.createElement('figcaption');
    const open = document.createElement('a');
    open.href = url.href;
    open.target = '_blank';
    open.rel = 'noopener noreferrer';
    open.textContent = name;
    open.title = url.pathname + url.search;
    const note = document.createElement('span');
    note.className = 'video-note';
    note.textContent = '视频';
    caption.append(open, note);

    // 远端地址可能不让直接播放（防盗链、需要登录）。别留个坏掉的框，给一句人话 + 原链接。
    const trouble = document.createElement('p');
    trouble.className = 'video-trouble';
    trouble.hidden = true;
    trouble.textContent = '这个地址不能直接播放，点上面的文件名可以在新标签打开。';
    video.addEventListener('error', () => {
      figure.classList.add('is-broken');
      trouble.hidden = false;
    });

    figure.append(video, caption, trouble);
    return figure;
  }

  function enhance(root) {
    if (!root || !root.querySelectorAll) return 0;
    let made = 0;
    root.querySelectorAll('a[href]').forEach(link => {
      if (link.dataset.videoDone === '1' || link.closest('.video-card')) return;
      let url = null;
      try { url = new URL(link.getAttribute('href') || '', location.origin); } catch (_error) { return; }
      if (!/^(https?|blob):$/.test(url.protocol)) return;
      const name = videoLikeName(url);
      if (!name) return;
      link.dataset.videoDone = '1';
      link.replaceWith(card(url, name));
      made += 1;
    });
    // 本地路径的视频不做播放器：经 SSH 拉一个 5MB 的 mp4 要十几秒，框一直空着更难受。
    // 路径本身还是 .plink，点开是文件面板/下载；要预览就看消息里的远端链接。
    return made;
  }

  window.TmuxVideo = {enhance, VIDEO_URL_RE};
})();
