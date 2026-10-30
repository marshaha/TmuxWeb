/*
 * Theme selection, applied before first paint so a dark theme never flashes cream.
 *
 *   <html data-theme="chatgpt">        ← written here, read by /assets/theme.css
 *   localStorage['tmux-theme'] = name  ← remembered per browser
 *
 * TmuxTheme.set(name) switches and re-broadcasts; the settings panel uses it.
 */
(() => {
  const KEY = 'tmux-theme';
  // `page`/`accent` only feed the swatch in the settings panel
  const THEMES = [
    { name: 'warm', label: '默认', hint: '暖灰 + 陶土橙', page: '#f7f7f5', accent: '#c96442' },
    { name: 'chatgpt', label: 'ChatGPT', hint: '白底 + 绿色强调', page: '#ffffff', accent: '#10a37f' },
    { name: 'claude', label: 'Claude', hint: '米白 + 陶土橙', page: '#faf9f5', accent: '#d97757' },
    { name: 'github', label: 'GitHub', hint: '白底 + 蓝色强调', page: '#ffffff', accent: '#0969da' },
    { name: 'claude-code', label: 'Claude Code', hint: '深色终端 + 橙色强调', page: '#1a1917', accent: '#d97757' },
    { name: 'night', label: '夜间', hint: '中性深灰 + 蓝色强调', page: '#15161a', accent: '#7aa2f7' },
  ];
  const known = new Set(THEMES.map(theme => theme.name));

  function read() {
    try {
      const saved = localStorage.getItem(KEY);
      return saved && known.has(saved) ? saved : 'warm';
    } catch {
      return 'warm';
    }
  }

  function paint(name) {
    const root = document.documentElement;
    if (name === 'warm') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', name);
  }

  function set(name) {
    const wanted = known.has(name) ? name : 'warm';
    try { localStorage.setItem(KEY, wanted); } catch { /* private mode: still applies for this page */ }
    paint(wanted);
    document.dispatchEvent(new CustomEvent('tmuxweb:theme', { detail: { name: wanted } }));
    return wanted;
  }

  paint(read());
  window.TmuxTheme = { get: read, set, list: () => THEMES.slice() };
})();
