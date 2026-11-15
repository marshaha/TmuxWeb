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

  // 先按原地址放：能直连的一律直连（少一跳、少占服务器带宽）。
  // 播不了才换成自家代理 —— openlist 这类签名链接常常发 octet-stream + attachment，
  // 浏览器拿到就拒播，签名又改不了。
  function directSrc(url) {
    return url.href;
  }

  function proxySrc(url) {
    if (!/^https?:$/.test(url.protocol)) return '';
    if (url.origin === location.origin) return '';
    return `${location.origin}/api/media/proxy?url=${encodeURIComponent(url.href)}`;
  }

  function card(url, name) {
    const figure = document.createElement('figure');
    figure.className = 'video-card';
    figure.dataset.videoSrc = url.href;

    const video = document.createElement('video');
    video.controls = true;
    video.playsInline = true;
    video.preload = 'metadata';
    video.setAttribute('controlsList', 'nodownload');
    // 加一个 #t=0.5 媒体片段：Chrome 会立刻解一帧画出来，否则 metadata 到了也还是黑框
    const withFrame = href => (href.includes('#') ? href : `${href}#t=0.5`);
    video.src = withFrame(directSrc(url));

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
    const fallback = document.createElement('a');
    fallback.href = url.href;
    fallback.target = '_blank';
    fallback.rel = 'noopener noreferrer';
    fallback.textContent = '在新标签打开';
    trouble.append('这个地址不能直接播放，', fallback, '。');
    video.addEventListener('error', () => {
      // 直连被拒（octet-stream / attachment / 防盗链）→ 换代理再来一次；代理也不行才算坏
      const proxied = proxySrc(url);
      if (proxied && video.dataset.proxied !== '1') {
        video.dataset.proxied = '1';
        video.src = withFrame(proxied);
        video.load();
        return;
      }
      figure.classList.add('is-broken');
      trouble.hidden = false;
    });

    figure.append(video, caption, trouble);
    return figure;
  }

  // 同一条消息里同一个地址只放一个播放器：enhance 可能被不同层级调用好几次
  // （整段重渲染、eager 处理单条消息…），只在自己的 root 里查会漏。
  function alreadyPlayed(root, href) {
    const scope = (root.closest && root.closest('.chat-message')) || root;
    return [...scope.querySelectorAll('.video-card')].some(card => card.dataset.videoSrc === href);
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
      if (alreadyPlayed(root, url.href)) return;
      const player = card(url, name);
      const block = link.closest('pre');
      if (block) block.after(player);
      else link.replaceWith(player);
      made += 1;
    });
    // agent 经常顺手贴一行裸 HTML：`<video controls src="…"></video>`。
    // markdown 会把它转义成文字（屏幕上就是一堆尖括号），这里摘掉，用同一个播放器代替。
    const RAW_VIDEO = /<video\b[^>]*?\bsrc=["']([^"']+)["'][^>]*>(?:\s*<\/video>)?/gi;
    root.querySelectorAll('pre').forEach(block => {
      const text = block.textContent || '';
      const found = [...text.matchAll(RAW_VIDEO)].map(match => match[1]);
      if (!found.length) return;
      // 摘掉标签，剩下的文字（说明行）留着
      const cleaned = text.replace(RAW_VIDEO, '').replace(/\n{3,}/g, '\n\n');
      if (!block.querySelector('.plink')) block.textContent = cleaned;
      else {
        // 里面已经有链接了：只删掉标签那一段文字节点
        [...block.childNodes].forEach(node => {
          if (node.nodeType === 3 && /<video\b/i.test(node.nodeValue || '')) {
            node.nodeValue = node.nodeValue.replace(RAW_VIDEO, '');
          }
        });
      }
      found.forEach(raw => {
        const target = raw.trim();
        if (!target) return;
        if (alreadyPlayed(root, target)) return;
        let url = null;
        try { url = new URL(target, location.origin); } catch (_error) { return; }
        if (!/^(https?|blob):$/.test(url.protocol)) return;
        const name = videoLikeName(url) || '视频';
        block.after(card(url, name));
        made += 1;
      });
    });

    // 本地路径的视频不做播放器：经 SSH 拉一个 5MB 的 mp4 要十几秒，框一直空着更难受。
    // 路径本身还是 .plink，点开是文件面板/下载；要预览就看消息里的远端链接。
    return made;
  }

  window.TmuxVideo = {enhance, VIDEO_URL_RE};
})();
