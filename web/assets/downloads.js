/* 下载确认层 —— 移动端下载前必须先确认。
 *
 * 为什么不直接点：iOS Safari 打开下载链接有时直接跳进系统文件预览，
 * 那个页面没有返回按钮，用户会“困”在里面回不来。所以触摸设备一律
 * 先弹一张小卡（文件名 + 大小 + 取消/下载），用户点了才真的去下。
 * 桌面端不打扰，保持点一下就下。
 *
 *   TmuxDownload.isMobile()
 *   TmuxDownload.maybe(href, {name, meta})   → 移动端先问，桌面直接下
 *   TmuxDownload.ask({name, meta, href})     → 总是问，返回 Promise<boolean>
 *   TmuxDownload.go(href)                    → 真的触发下载
 */
(function () {
  const MOBILE = /Android|iPhone|iPad|iPod|Windows Phone/i.test(navigator.userAgent)
    || (window.matchMedia && window.matchMedia('(hover: none) and (pointer: coarse)').matches);

  let sheet = null;
  let resolveAsk = null;

  function isMobile() { return MOBILE; }

  function go(href) {
    const link = document.createElement('a');
    link.href = href;
    link.rel = 'noopener';
    link.setAttribute('download', '');
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  function close(result) {
    const done = resolveAsk;
    resolveAsk = null;
    if (sheet) {
      sheet.hidden = true;
      sheet.classList.remove('on');
    }
    if (done) done(!!result);
  }

  function ensureSheet() {
    if (sheet) return sheet;
    sheet = document.createElement('div');
    sheet.className = 'dl-sheet';
    sheet.hidden = true;
    sheet.innerHTML = ''
      + '<div class="dl-sheet-card" role="dialog" aria-modal="true" aria-labelledby="dl-title">'
      + '<div class="dl-sheet-title" id="dl-title">下载文件</div>'
      + '<div class="dl-sheet-name"></div>'
      + '<div class="dl-sheet-meta"></div>'
      + '<div class="dl-sheet-note">下载后会存到「文件」App；iOS 上也可能直接打开预览，用左上角返回。</div>'
      + '<div class="dl-sheet-actions">'
      + '<button type="button" class="dl-btn" data-dl="cancel">取消</button>'
      + '<button type="button" class="dl-btn primary" data-dl="ok">下载</button>'
      + '</div></div>';
    sheet.addEventListener('click', event => {
      const button = event.target.closest('[data-dl]');
      if (button) { close(button.dataset.dl === 'ok'); return; }
      if (event.target === sheet) close(false);       // 点遮罩 = 取消
    });
    sheet.addEventListener('keydown', event => { if (event.key === 'Escape') close(false); });
    document.body.appendChild(sheet);
    return sheet;
  }

  function ask(options) {
    const { href, name, meta } = options || {};
    const panel = ensureSheet();
    panel.querySelector('.dl-sheet-name').textContent = name || '未命名文件';
    panel.querySelector('.dl-sheet-meta').textContent = meta || '';
    panel.querySelector('.dl-sheet-meta').hidden = !meta;
    panel.hidden = false;
    requestAnimationFrame(() => panel.classList.add('on'));
    panel.querySelector('[data-dl="ok"]').focus();
    return new Promise(resolve => {
      resolveAsk = accepted => { if (accepted && href) go(href); resolve(!!accepted); };
    });
  }

  function maybe(href, options) {
    if (!MOBILE) { go(href); return Promise.resolve(true); }
    return ask({ ...(options || {}), href });
  }

  window.TmuxDownload = { isMobile, maybe, ask, go };
})();
