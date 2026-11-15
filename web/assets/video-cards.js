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

  function card(url, name) {
    const figure = document.createElement('figure');
    figure.className = 'video-card';

    const video = document.createElement('video');
    video.controls = true;
    video.playsInline = true;
    video.preload = 'metadata';
    video.setAttribute('controlsList', 'nodownload');
    video.src = url.href;
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
    // 聊天里的路径（普通文本或代码块里的 /path/to/x.mp4）会被 path-links 变成 .plink，
    // href 是 "#"，上面的循环认不出来 —— 这里按路径再认一次，就地给播放器。
    root.querySelectorAll('.plink[data-plink-path]').forEach(link => {
      if (link.dataset.videoDone === '1' || link.closest('.video-card')) return;
      const path = link.dataset.plinkPath || '';
      if (!VIDEO_URL_RE.test(path)) return;
      const pane = link.dataset.plinkPane || '';
      if (!pane) return;
      let url = null;
      try {
        url = new URL(`/api/panes/${encodeURIComponent(pane)}/files/raw?path=${encodeURIComponent(path)}`,
                      location.origin);
      } catch (_error) { return; }
      link.dataset.videoDone = '1';
      const player = card(url, path.split('/').filter(Boolean).pop() || '视频');
      // 代码块里插播放器会被 <pre> 的 monospace + nowrap 挤歪：路径留在块里当链接，
      // 播放器放到块下面
      const block = link.closest('pre');
      if (block) block.after(player);
      else link.replaceWith(player);
      made += 1;
    });
    return made;
  }

  window.TmuxVideo = {enhance, VIDEO_URL_RE};
})();
