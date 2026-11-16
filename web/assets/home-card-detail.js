// Detail panel for board result/alert/note cards (home.html #cards).
//
// A card body already ships in full in the /api/home payload, but the card
// view clamps it to six lines and these cards have no pane to preview, so
// the rest of the text was unreachable from the board. Clicking one now
// opens the page's preview drawer (#preview-drawer) with the full body.
//
// The drawer, its backdrop, its close button and the Esc handling are the
// same ones the pane full preview uses (home.html closeFullPreview); this
// module only fills the drawer with card content and swaps the iframe for
// the text body. Opening never fetches: the body comes from the card the
// caller already has. Closing stays with closeFullPreview, which calls
// resetDetail() after the slide-out to hand the drawer back clean.
(function (global) {
  'use strict';

  function findCard(cards, id) {
    if (!Array.isArray(cards) || id == null || id === '') return null;
    for (const card of cards) {
      if (card && String(card.id) === String(id)) return card;
    }
    return null;
  }

  function openDetail(doc, card, deps) {
    if (!card) return false;
    const drawer = doc.getElementById('preview-drawer');
    const frame = doc.getElementById('pd-frame');
    const backdrop = doc.getElementById('pd-backdrop');
    const title = doc.getElementById('pd-title');
    const consoleLink = doc.getElementById('pd-console');
    const body = doc.getElementById('pd-body');
    const kind = doc.getElementById('pd-kind');
    const time = doc.getElementById('pd-time');
    const text = doc.getElementById('pd-text');
    const foot = doc.getElementById('pd-foot');
    const paneLink = doc.getElementById('pd-open-pane');
    if (!drawer || !body || !text) return false;
    if (drawer.setAttribute) drawer.setAttribute('aria-label', '卡片详情');
    if (title) title.textContent = card.title || '';
    if (kind) {
      kind.textContent = card.kind || '';
      kind.className = 'kind ' + (card.kind || '');
    }
    if (time) time.textContent = deps.formatTime ? deps.formatTime(card.created_at) : '';
    // the whole body, no clamp: the card view is what truncated it
    text.innerHTML = deps.mdCard ? deps.mdCard(card.body || '') : '';
    const paneUrl = card.pane_id && deps.consoleUrl ? deps.consoleUrl(card.pane_id) : '';
    if (consoleLink) {
      if (paneUrl) {
        consoleLink.href = paneUrl;
        consoleLink.hidden = false;
      } else {
        consoleLink.hidden = true;
      }
    }
    if (foot) foot.hidden = !paneUrl;
    if (paneLink && paneUrl) paneLink.href = paneUrl;
    if (frame) frame.hidden = true;
    body.hidden = false;
    drawer.hidden = false;
    const raf = typeof requestAnimationFrame === 'function'
      ? requestAnimationFrame
      : function (fn) { fn(); };
    raf(function () { drawer.classList.add('open'); });
    if (backdrop) backdrop.hidden = false;
    if (doc.body) doc.body.classList.add('drawer-open');
    return true;
  }

  function resetDetail(doc) {
    // Called by home.html closeFullPreview after the slide-out, so the next
    // pane preview gets a clean drawer. Skipped while the drawer is open
    // again: a close followed by a quick reopen must not blank the new panel.
    const drawer = doc.getElementById('preview-drawer');
    if (drawer && drawer.classList.contains('open')) return;
    const body = doc.getElementById('pd-body');
    const text = doc.getElementById('pd-text');
    const frame = doc.getElementById('pd-frame');
    if (body) body.hidden = true;
    if (text) text.innerHTML = '';
    if (frame) frame.hidden = false;
    if (drawer && drawer.setAttribute) drawer.setAttribute('aria-label', '对话完整预览');
  }

  global.TmuxHomeCardDetail = {
    findCard: findCard,
    openDetail: openDetail,
    resetDetail: resetDetail,
  };
})(typeof window !== 'undefined' ? window : globalThis);
