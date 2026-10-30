/*
 * Sidebar buttons contributed by plugins.
 *
 * Loaded by the console, the board and the multi-window page. It asks /api/plugins which buttons are live
 * (a disabled plugin contributes nothing) and appends them to the icon row of that page, so a plugin can put
 * an entry point next to 主页 / 多窗口 without editing any HTML.
 */
(() => {
  const ROW_SELECTOR = '.brand-actions, .side-actions';
  const RENDERED = 'data-plugin-button';

  function iconFor(row, name) {
    // reuse the page's own sprite when it has the symbol, otherwise fall back to the plug glyph
    const symbol = document.getElementById(`i-${name}`) ? `#i-${name}` : '#i-plug';
    return `<svg class="ico"><use href="${symbol}"/></svg>`;
  }

  function mount(buttons) {
    const rows = [...document.querySelectorAll(ROW_SELECTOR)];
    if (!rows.length) return;
    for (const row of rows) {
      for (const old of row.querySelectorAll(`[${RENDERED}]`)) old.remove();
      for (const button of buttons) {
        const link = document.createElement('a');
        link.className = 'icon-btn';
        link.href = button.href;
        link.title = button.title || button.label;
        link.setAttribute('aria-label', button.label);
        link.setAttribute('data-plugin-button', button.plugin);
        link.innerHTML = iconFor(row, button.icon);
        row.appendChild(link);
      }
    }
  }

  async function refresh() {
    try {
      const response = await fetch('/api/plugins', { credentials: 'same-origin' });
      if (!response.ok) return;                       // logged out or no plugin API: nothing to add
      const data = await response.json();
      mount(Array.isArray(data.buttons) ? data.buttons : []);
    } catch {
      /* a sidebar button is never worth an error banner */
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refresh);
  else refresh();
  document.addEventListener('tmuxweb:plugins-changed', refresh);   // the settings panel can ask for a redraw
})();
