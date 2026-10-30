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

  // The account's theme rides in a cookie the server sets (login + every save), so a second device paints in
  // the right palette immediately; localStorage stays as the fallback for the login page and for browsers
  // that never synced.
  function fromCookie() {
    const match = document.cookie.match(/(?:^|;\s*)tmux_theme=([^;]+)/);
    if (!match) return '';
    const name = decodeURIComponent(match[1]);
    return known.has(name) ? name : '';
  }

  function read() {
    const fromServer = fromCookie();
    if (fromServer) return fromServer;
    try {
      const saved = localStorage.getItem(KEY);
      return saved && known.has(saved) ? saved : 'warm';
    } catch {
      return 'warm';
    }
  }

  function writeCookie(name) {
    try {
      document.cookie = name === 'warm'
        ? 'tmux_theme=; Max-Age=0; path=/'
        : `tmux_theme=${encodeURIComponent(name)}; Max-Age=31536000; path=/; SameSite=Lax`;
    } catch { /* cookies disabled: the fallback below still works for this session */ }
  }

  // Reconcile with the account: a device that has not seen this console before picks up the stored theme.
  async function syncWithAccount() {
    try {
      const response = await fetch('/api/prefs', { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) return;                       // logged out: nothing to reconcile
      const data = await response.json();
      const stored = (data.prefs || {}).theme;
      if (stored && known.has(stored) && stored !== read()) {
        writeCookie(stored);
        try { localStorage.setItem(KEY, stored); } catch {}
        paint(stored);
        document.dispatchEvent(new CustomEvent('tmuxweb:theme', { detail: { name: stored } }));
      }
    } catch { /* offline: keep what we painted */ }
  }

  // Persist to the account; the optimistic paint below means the switch feels instant even on a slow link.
  async function saveToAccount(name) {
    if (!window.fetch) return;
    try {
      await fetch('/api/prefs', {
        method: 'PUT',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ theme: name }),
      });
    } catch { /* the local copy is already applied; the next page load retries */ }
  }

  function paint(name) {
    const root = document.documentElement;
    if (name === 'warm') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', name);
  }

  function set(name) {
    const wanted = known.has(name) ? name : 'warm';
    try { localStorage.setItem(KEY, wanted); } catch { /* private mode: still applies for this page */ }
    writeCookie(wanted);
    paint(wanted);
    document.dispatchEvent(new CustomEvent('tmuxweb:theme', { detail: { name: wanted } }));
    saveToAccount(wanted);
    return wanted;
  }

  paint(read());
  syncWithAccount();
  window.TmuxTheme = { get: read, set, list: () => THEMES.slice() };
})();
