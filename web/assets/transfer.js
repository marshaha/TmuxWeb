/* 直连传输：页面留在公网（Cloudflare）域名，字节走更快的直连源站。
 *
 * 为什么要这层：CF 隧道对单请求限制 100MB / 100 秒，慢链路上大文件传不动。
 * 但读者往往就在同一个局域网 / tailnet 里，直连服务器 IP 又快又没有任何限制。
 * 让人记第二个地址不现实（cookie 是 host-only，换了域名还要重新登录），所以
 * 改成"页面不动、只把字节搬过去"。
 *
 * 直连源站读不到会话 cookie，鉴权换成服务端签的短命票据（绑定 pane/路径/方向/
 * 大小，10 分钟过期）。探测一次就记住，连不通就静默回落，用户看不到差别。
 *
 *   TmuxTransfer.downloadHref(href)                    → Promise<真实下载地址>
 *   TmuxTransfer.uploadRoute({pane,dir,name,size})     → Promise<{prefix,ticket}|null>
 *   TmuxTransfer.forget()                              → 清掉记住的源站
 */
(function () {
  const STORE_KEY = 'tmuxweb.transfer.origin';
  const STORE_TTL = 24 * 3600 * 1000;
  const PROBE_TIMEOUT = 1500;        // 探测一次最多等 1.5 秒，超时就当不通

  let pending = null;                // 探测进行中，避免并发重复探测
  let missAt = 0;                    // 上次"没有可用直连"的时间：5 分钟内不再试

  let cachedRoute = null;

  function readCache() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
      if (!raw || !raw.origin || Date.now() - raw.at > STORE_TTL) return null;
      cachedRoute = raw.route || null;
      return raw.origin;
    } catch (_error) { return null; }
  }

  function writeCache(origin, route) {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({origin, at: Date.now(), route: route || null}));
    } catch (_error) {}
  }

  function forget() {
    cachedRoute = null;
    try { localStorage.removeItem(STORE_KEY); } catch (_error) {}
    pending = null;
    missAt = 0;
  }

  async function probe(origin) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT);
    try {
      const response = await fetch(origin + '/t/ping', {
        method: 'GET', mode: 'cors', cache: 'no-store', credentials: 'omit',
        signal: controller.signal,
      });
      if (!response.ok) return false;
      const payload = await response.json().catch(() => null);
      return !!(payload && payload.tmuxweb);
    } catch (_error) {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  // 旧版只有一个开/关（tmuxweb.transfer.direct）。现在只有"线路"这一个概念：
  // off 等价于 cf。旧键留着只为读出用户上一次的选择。
  const DIRECT_KEY = 'tmuxweb.transfer.direct';

  const ROUTE_KEY = 'tmuxweb.transfer.route';

  // 下载“浏览器 → 本机”这一跳走哪条：ts（tailnet，默认）/ lan（局域网）/ cf（当前页面）
  function routePref() {
    try {
      const value = localStorage.getItem(ROUTE_KEY);
      if (value === 'lan' || value === 'cf' || value === 'ts') return value;
      // 还没有线路偏好：老版本那个开关说了算（off = 走当前页面）
      return localStorage.getItem(DIRECT_KEY) === 'off' ? 'cf' : 'ts';
    } catch (_error) {
      return 'ts';
    }
  }

  let prefsSynced = false;

  // 线路跟账号走：本机还没存过线路时，把账号里的偏好搬下来。
  // 否则一个旧浏览器里残留的 transfer=off 会一直把下载压在 CF 上，界面上还看不出来。
  async function syncPrefsOnce() {
    if (prefsSynced) return;
    prefsSynced = true;
    try {
      if (localStorage.getItem(ROUTE_KEY)) return;
      const response = await fetch('/api/prefs', {credentials: 'same-origin'});
      if (!response.ok) return;
      const data = await response.json();
      const stored = (data.prefs || {}).transfer;
      if (stored !== 'on' && stored !== 'off') return;
      localStorage.setItem(ROUTE_KEY, stored === 'off' ? 'cf' : 'ts');
      // 顶栏那个按钮是初始化时画的，线路被纠正后要让它跟着改
      try { window.dispatchEvent(new CustomEvent('tmux-route-change')); } catch (_error) {}
    } catch (_error) { /* 离线：按本机已有的判断 */ }
  }

  function setRoutePref(value) {
    const next = value === 'lan' || value === 'cf' ? value : 'ts';
    try { localStorage.setItem(ROUTE_KEY, next); } catch (_error) {}
    return next;
  }

  // 同一条线路内部也有先后：tailnet 的 MagicDNS（https 名字）最先，
  // 其次是 tailnet 的 100.x，最后才是局域网地址
  function rankOf(item) {
    const isTailnet = item.kind === 'tailnet';
    const isHttps = String(item.url).startsWith('https://');
    if (isTailnet && isHttps) return 0;
    if (isTailnet) return 1;
    if (item.kind === 'lan') return 2;
    return 3;
  }

  async function origins() {
    const wanted = routePref();
    if (wanted === 'cf') return [];          // 明确走当前页面（CF）：不探测、不改道
    // 只考虑 https 的直连源站：https 页面对 http 的 fetch 和下载都会被浏览器拦。
    try {
      const response = await fetch('/api/system/addresses', {credentials: 'same-origin'});
      if (!response.ok) return [];
      const data = await response.json();
      // 页面本身就在这个源站上（用户直接用 tailnet 名字打开的）→ 不需要改道，
      // 同源传输本来就是直连，别白白多签一张票据
      return (data.addresses || [])
        .filter(item => item.transfer && item.url !== location.origin)
        .filter(item => (wanted === 'lan' ? item.kind === 'lan' : item.kind === 'tailnet'))
        .sort((left, right) => rankOf(left) - rankOf(right))
        .map(item => item.url);
    } catch (_error) {
      return [];
    }
  }

  const MISS_TTL = 5 * 60 * 1000;
  const CACHE_TRUST = 60 * 1000;      // 记住的源站，确认过一次之后 60 秒内不再探测
  let verifiedAt = 0;

  async function pick() {
    // 明确选了 CF：直接不改道，也别记"没探到"，否则会把 5 分钟内的 ts 一起挡掉
    await syncPrefsOnce();
    if (routePref() === 'cf') return null;
    const cached = readCache();
    const stale = cached && cachedRoute !== null && cachedRoute !== routePref();
    if (cached && stale) forget();          // 换了线路：记住的旧源站不算数
    if (cached && !stale) {
      // 记住的源站不能盲信：换了网络（离开 tailnet）、证书过期、隧道关掉，
      // 那个地址就变成"点了下载什么都不发生"——页面不报错，文件永远不来。
      if (Date.now() - verifiedAt < CACHE_TRUST) return cached;
      if (await probe(cached)) { verifiedAt = Date.now(); return cached; }
      forget();                       // 连不上了：清掉，回去走页面自己的地址
    }
    if (missAt && Date.now() - missAt < MISS_TTL) return null;   // 没内网入口时别每次下载都探
    if (pending) return pending;
    pending = (async () => {
      const chosenRoute = routePref();
      for (const origin of await origins()) {
        if (await probe(origin)) {
          writeCache(origin, chosenRoute); verifiedAt = Date.now(); missAt = 0; return origin;
        }
      }
      missAt = Date.now();
      return null;
    })();
    try { return await pending; } finally { pending = null; }
  }

  async function ticket(body) {
    const response = await fetch('/api/transfer/ticket', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    if (!response.ok) {
      let detail = 'HTTP ' + response.status;
      try { detail = (await response.json()).detail || detail; } catch (_error) {}
      throw new Error(detail);
    }
    return response.json();
  }

  // 普通下载链接 → 直连源站上的票据链接（拿不到就原样返回）
  async function downloadHref(href) {
    let parsed;
    try { parsed = new URL(href, location.href); } catch (_error) { return href; }
    const match = parsed.pathname.match(/^\/api\/panes\/([^/]+)\/files\/download$/);
    if (!match || !parsed.searchParams.get('path')) return href;
    const origin = await pick();
    if (!origin) return href;
    try {
      const grant = await ticket({
        pane: decodeURIComponent(match[1]),
        path: parsed.searchParams.get('path'),
        direction: 'down',
      });
      return origin + '/t/download?t=' + encodeURIComponent(grant.token);
    } catch (_error) {
      return href;                     // 票据拿不到：当作没有直连，别打断下载
    }
  }

  // 打包下载：签一张 zip 票据，返回直连源站上的地址；拿不到就 null（调用方走原来的 blob 路径）
  async function zipHref(info) {
    const origin = await pick();
    if (!origin) return null;
    try {
      const grant = await ticket({
        pane: info.pane,
        path: info.base || '',
        direction: 'zip',
        paths: info.paths || [],
      });
      if (!grant || !grant.token) return null;
      return origin + '/t/zip?t=' + encodeURIComponent(grant.token);
    } catch (_error) {
      return null;
    }
  }

  // 上传：返回 {prefix, ticket} 让 uploads.js 把块发到直连源站；null 表示保持原样
  async function uploadRoute(options) {
    const origin = await pick();
    if (!origin) return null;
    try {
      const grant = await ticket({
        pane: options.pane, path: '/' + options.name, direction: 'up',
        size: options.size, name: options.name,
      });
      return {prefix: origin, ticket: grant.token};
    } catch (_error) {
      return null;
    }
  }

  window.TmuxTransfer = {downloadHref, zipHref, uploadRoute, pick, forget, probe,
                         routePref, setRoutePref};
})();
