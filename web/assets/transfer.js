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

  function readCache() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
      if (!raw || !raw.origin || Date.now() - raw.at > STORE_TTL) return null;
      return raw.origin;
    } catch (_error) { return null; }
  }

  function writeCache(origin) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({origin, at: Date.now()})); } catch (_error) {}
  }

  function forget() {
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

  const DIRECT_KEY = 'tmuxweb.transfer.direct';

  // 默认开（和以前一样）：本机/SSH 主机的下载改走局域网或 Tailscale；
  // 想一律走当前页面（CF），在 设置 › 直连地址 › 文件下载走直连源站 关掉。
  // 子节点的 pane 不看这个开关，直接按子节点自己的线路走。
  function directEnabled() {
    try {
      return localStorage.getItem(DIRECT_KEY) !== 'off';
    } catch (_error) {
      return true;
    }
  }

  const ROUTE_KEY = 'tmuxweb.transfer.route';

  // 下载“浏览器 → 本机”这一跳走哪条：ts（tailnet，默认）/ lan（局域网）/ cf（当前页面）
  function routePref() {
    try {
      const value = localStorage.getItem(ROUTE_KEY);
      return value === 'lan' || value === 'cf' ? value : 'ts';
    } catch (_error) {
      return 'ts';
    }
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
    if (!directEnabled()) return [];
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
    const cached = readCache();
    if (cached) {
      // 记住的源站不能盲信：换了网络（离开 tailnet）、证书过期、隧道关掉，
      // 那个地址就变成"点了下载什么都不发生"——页面不报错，文件永远不来。
      if (Date.now() - verifiedAt < CACHE_TRUST) return cached;
      if (await probe(cached)) { verifiedAt = Date.now(); return cached; }
      forget();                       // 连不上了：清掉，回去走页面自己的地址
    }
    if (missAt && Date.now() - missAt < MISS_TTL) return null;   // 没内网入口时别每次下载都探
    if (pending) return pending;
    pending = (async () => {
      for (const origin of await origins()) {
        if (await probe(origin)) { writeCache(origin); verifiedAt = Date.now(); missAt = 0; return origin; }
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

  // 子节点的 pane 编号是 `<node_id>~%12`：它的字节按子节点自己的线路走
  // （先它存的域名，域名不通再试它的内网/tailnet 地址），不该被改道到本机的直连源站
  function isFederatedPane(pane) {
    const text = String(pane || '');
    return text.includes('~') || /%7e/i.test(text) || /%257e/i.test(text);
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

  window.TmuxTransfer = {downloadHref, uploadRoute, pick, forget, probe,
                         routePref, setRoutePref};
})();
