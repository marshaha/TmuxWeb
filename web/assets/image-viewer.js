/* In-chat image dialog. Keep zoom and pan within the stage. */
(function () {
  const MIN_SCALE = 1;
  const MAX_SCALE = 6;

  let root = null;
  let stage = null;
  let image = null;
  let label = null;
  let zoomButton = null;
  let openLink = null;
  let scale = 1;
  let offsetX = 0;
  let offsetY = 0;
  let pinching = null;
  let dragging = null;
  let pushed = false;

  function ensure() {
    if (root) return root;
    root = document.createElement('dialog');
    root.className = 'iview';
    root.id = 'iview';
    root.setAttribute('aria-labelledby', 'iview-name');
    root.innerHTML = '<header class="iview-head"><strong id="iview-name">图片</strong>'
      + '<div class="iview-tools">'
      + '<button class="iview-btn" type="button" id="iview-zoom" aria-pressed="false" aria-label="放大图片">放大</button>'
      + '<a class="iview-btn" id="iview-open" target="_blank" rel="noopener noreferrer">原图</a>'
      + '<button class="iview-btn" type="button" id="iview-close" data-iview-close>关闭</button>'
      + '</div></header>'
      + '<div class="iview-stage" id="iview-stage"><img id="iview-img" alt=""></div>';
    document.body.appendChild(root);
    stage = root.querySelector('#iview-stage');
    image = root.querySelector('#iview-img');
    label = root.querySelector('#iview-name');
    zoomButton = root.querySelector('#iview-zoom');
    openLink = root.querySelector('#iview-open');

    function outside(event) {
      const box = root.getBoundingClientRect();
      return event.clientX < box.left || event.clientX >= box.right ||
        event.clientY < box.top || event.clientY >= box.bottom;
    }
    let outsideDown = false;
    root.addEventListener('pointerdown', event => { outsideDown = event.target === root && outside(event); });
    root.addEventListener('click', event => {
      if (event.target.closest('[data-iview-close]') || (outsideDown && event.target === root && outside(event))) close();
      outsideDown = false;
    });
    root.addEventListener('cancel', event => { event.preventDefault(); close(); });
    zoomButton.addEventListener('click', () => {
      if (scale > 1.01) apply(1, 0, 0);
      else apply(2, 0, 0);
    });
    stage.addEventListener('dblclick', event => {
      event.preventDefault();
      if (scale > 1.01) {
        apply(1, 0, 0);
        return;
      }
      const box = stage.getBoundingClientRect();
      const x = event.clientX - box.left - box.width / 2;
      const y = event.clientY - box.top - box.height / 2;
      apply(2.4, -x * 1.4, -y * 1.4);
    });
    stage.addEventListener('wheel', event => {
      if (!root?.open) return;
      event.preventDefault();
      const next = clamp(scale * (event.deltaY < 0 ? 1.12 : 1 / 1.12));
      apply(next, offsetX, offsetY);
    }, {passive: false});

    // 触摸：单指拖动 / 双指捏合（不用 CSS pinch-zoom，iOS 上会把整个页面一起缩放）
    stage.addEventListener('pointerdown', event => {
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      stage.setPointerCapture(event.pointerId);
      const pointers = [...stage.__pointers || []];
      pointers.push({id: event.pointerId, x: event.clientX, y: event.clientY});
      stage.__pointers = pointers;
      if (pointers.length === 1) {
        dragging = {x: event.clientX, y: event.clientY, ox: offsetX, oy: offsetY, moved: false};
      } else if (pointers.length === 2) {
        dragging = null;
        const [a, b] = pointers;
        pinching = {distance: Math.hypot(a.x - b.x, a.y - b.y), scale};
      }
    });
    stage.addEventListener('pointermove', event => {
      const pointers = stage.__pointers || [];
      const entry = pointers.find(item => item.id === event.pointerId);
      if (entry) { entry.x = event.clientX; entry.y = event.clientY; }
      if (pinching && pointers.length === 2) {
        const [a, b] = pointers;
        const distance = Math.hypot(a.x - b.x, a.y - b.y);
        if (pinching.distance > 0) apply(pinching.scale * (distance / pinching.distance), offsetX, offsetY);
        return;
      }
      if (dragging) {
        const dx = event.clientX - dragging.x;
        const dy = event.clientY - dragging.y;
        if (Math.abs(dx) + Math.abs(dy) > 4) dragging.moved = true;
        if (scale > 1.01) apply(scale, dragging.ox + dx, dragging.oy + dy);
      }
    });
    const release = event => {
      stage.__pointers = (stage.__pointers || []).filter(item => item.id !== event.pointerId);
      if ((stage.__pointers || []).length < 2) pinching = null;
      if ((stage.__pointers || []).length === 0) {
        // 放大状态下点一下空白 = 回到适应屏幕（手机上比找按钮快）
        if (dragging && !dragging.moved && scale > 1.01) apply(1, 0, 0);
        dragging = null;
      }
    };
    stage.addEventListener('pointerup', release);
    stage.addEventListener('pointercancel', release);

    window.addEventListener('popstate', () => {
      if (root.open) close(true);
    });
    return root;
  }

  function clamp(value) {
    return Math.max(MIN_SCALE, Math.min(MAX_SCALE, value));
  }

  function apply(nextScale, x, y) {
    scale = clamp(nextScale);
    const limit = Math.max(0, (image.clientWidth * scale - stage.clientWidth) / 2);
    const limitY = Math.max(0, (image.clientHeight * scale - stage.clientHeight) / 2);
    offsetX = scale <= 1.01 ? 0 : Math.max(-limit, Math.min(limit, x));
    offsetY = scale <= 1.01 ? 0 : Math.max(-limitY, Math.min(limitY, y));
    image.style.transform = `translate(${offsetX}px, ${offsetY}px) scale(${scale})`;
    root.classList.toggle('zoomed', scale > 1.01);
    if (zoomButton) {
      zoomButton.textContent = scale > 1.01 ? `${scale.toFixed(1)}×` : '放大';
      zoomButton.setAttribute('aria-label', scale > 1.01 ? '还原图片' : '放大图片');
      zoomButton.setAttribute('aria-pressed', String(scale > 1.01));
    }
  }

  function open(url, name) {
    const source = new URL(url, document.baseURI);
    if (!['http:', 'https:', 'blob:'].includes(source.protocol) && !/^data:image\//i.test(url)) return;
    ensure();
    label.textContent = name || '图片';
    openLink.hidden = !['http:', 'https:'].includes(source.protocol);
    if (openLink.hidden) openLink.removeAttribute('href');
    else openLink.href = source.href;
    image.alt = name || '';
    image.src = url;
    if (!root.open) root.showModal();
    apply(1, 0, 0);
    // 系统返回（安卓返回键 / iOS 侧滑）先关这一层，而不是退出整个控制台
    if (!pushed) {
      try {
        history.pushState({iview: 1}, '');
        pushed = true;
      } catch (_error) { pushed = false; }
    }
  }

  function close(fromPop) {
    if (!root?.open) return;
    root.close();
    stage.__pointers = [];
    pinching = dragging = null;
    image.removeAttribute('src');
    if (pushed && !fromPop) {
      pushed = false;
      try { history.back(); } catch (_error) { /* 无所谓 */ }
      return;
    }
    pushed = false;
  }

  // 气泡里点上传的图片 → 应用内查看，不要跳到裸图片页（手机上是白屏）
  document.addEventListener('click', event => {
    const thumb = event.target.closest ? event.target.closest('a.upload-thumb[data-upload]') : null;
    const inline = event.target.closest?.('#transcript-messages .chat-message img, #messages article img');
    if ((!thumb && !inline) || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (thumb?.classList.contains('broken')) {
      // 文件已经不在了：别跳过去白屏，什么都不做（气泡上本来就写着「图片已不在这里」）
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    const url = thumb?.getAttribute('href') || inline?.currentSrc || inline?.src;
    if (!url) return;
    event.preventDefault();
    event.stopPropagation();
    const name = thumb?.title || inline?.alt || inline?.title || '图片';
    open(url, name);
  });

  window.TmuxImageViewer = {open, close};
})();
