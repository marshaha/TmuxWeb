/*
 * The settings panel, shared by every page that has a "设置" button.
 *
 * It renders its own markup and styles so a page only needs:
 *     <script src="/assets/settings.js"></script>
 *     <button id="settings-open">设置</button>
 * and TmuxSettings.mount() wires it up.
 *
 * Contents: change the console password, export/import configuration (one section, two tabs), and
 * log out — the sidebar footer no longer carries a 退出 button.
 */
(() => {
  const STYLE = `
  .ts-direct-row{display:flex;align-items:center;gap:8px;margin:6px 0;padding:6px 8px;border:1px solid var(--line);border-radius:9px;background:var(--surface2)}
  .ts-direct-row code{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:11.5px ui-monospace,SFMono-Regular,Menlo,monospace}
  .ts-direct-kind{flex:none;padding:1px 5px;border-radius:5px;background:var(--accent);color:#fff;font-size:10px;letter-spacing:.04em}
  .ts-copy{flex:none;height:24px;padding:0 8px;font-size:11px}
  .ts-dialog{width:min(calc(100vw - 24px),560px);max-width:560px;max-height:86dvh;overflow:auto;padding:0;
    border:1px solid var(--line,#dddcd5);border-radius:18px;background:var(--card);color:var(--ink,#242421);
    box-shadow:0 24px 80px #0003;font:14px/1.5 system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif}
  .ts-dialog::backdrop{background:rgba(32,31,28,.28)}
  .ts-form{padding:0 0 4px}
  .ts-form h2{margin:0;padding:18px 20px 8px;font:500 18px/1.3 Georgia,serif}
  .ts-block{padding:0 20px}
  .ts-row{display:flex;width:100%;align-items:center;justify-content:space-between;padding:10px 0 8px;
    border:0;border-bottom:1px solid var(--line,#dddcd5);background:none;color:inherit;font:600 13px/1.4 inherit;cursor:pointer}
  .ts-row:hover{color:var(--accent,#c96442)}
  /* rows can be links (server / share management): same look, no underline, a chevron instead of a caret */
  .ts-row.ts-link{text-decoration:none}
  .ts-select{height:26px;padding:0 6px;border:1px solid var(--line,#dddcd5);border-radius:8px;
    background:var(--card,#fff);color:var(--ink,#242421);font:12px inherit}
  .ts-select:focus-visible{outline:2px solid var(--accent,#c96442);outline-offset:2px}
  .ts-row.ts-static{cursor:default}
  .ts-row.ts-static:hover{color:var(--ink,#242421)}
  .ts-row.ts-static + .ts-hint{margin:0 0 4px;padding-bottom:6px}
  .ts-go{flex:none;width:7px;height:7px;border-top:1.6px solid currentColor;border-right:1.6px solid currentColor;
    transform:rotate(45deg)}
  /* the panel focus goes to the dialog itself, so nothing is ringed when it opens; keyboard users still
     get a brand-coloured ring instead of the browser's blue one */
  .ts-dialog{outline:none}
  .ts-dialog:focus{outline:none}
  .ts-dialog button:focus-visible,.ts-dialog a:focus-visible{outline:2px solid var(--accent,#c96442);outline-offset:2px;border-radius:6px}
  .ts-caret{width:7px;height:7px;border-right:1.6px solid currentColor;border-bottom:1.6px solid currentColor;
    transform:rotate(45deg);transition:transform .15s}
  .ts-row[aria-expanded="true"] .ts-caret{transform:rotate(-135deg)}
  .ts-panel{padding:8px 0 4px}
  .ts-panel[hidden]{display:none}
  .ts-hint{margin:0 0 10px;color:var(--muted,#73716b);font-size:12px;line-height:1.6}
  .ts-field{display:flex;flex-direction:column;gap:5px;margin-bottom:10px;font-size:12px;color:var(--muted,#73716b)}
  .ts-field input{height:36px;padding:0 10px;border:1px solid var(--line,#dddcd5);border-radius:9px;background:var(--card);
    color:inherit;font:13px inherit;outline:none}
  .ts-field input:focus{border-color:var(--accent,#c96442)}
  .ts-state{margin:6px 0 0;min-height:16px;color:var(--muted,#73716b);font-size:12px}
  .ts-state.good{color:var(--green,#4f9a65)}
  .ts-state.bad{color:var(--danger,#b34b43)}
  .ts-tabs{display:inline-flex;gap:2px;padding:2px;margin-bottom:10px;border:1px solid var(--line,#dddcd5);
    border-radius:9px;background:var(--surface2)}
  .ts-tabs button{height:26px;padding:0 12px;border:0;border-radius:7px;background:transparent;
    color:var(--muted,#73716b);font:12px inherit;cursor:pointer}
  .ts-tabs button.on{background:#292825;color:#fff}
  .ts-btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:2px}
  .ts-btn{height:30px;padding:0 12px;border:1px solid var(--line,#dddcd5);border-radius:9px;background:var(--card);
    color:inherit;font:12px inherit;cursor:pointer}
  .ts-btn:hover{border-color:#c8c5bc;background:var(--surface2)}
  .ts-btn.primary{border-color:#292825;background:#292825;color:#fff}
  .ts-btn.primary:hover{background:#3a3934}
  .ts-btn.danger{color:var(--danger,#b34b43);border-color:#eed6cf}
  .ts-btn.danger:hover{background:color-mix(in srgb,var(--danger) 12%,var(--card))}
  .ts-file{display:flex;align-items:center;gap:10px;margin:0 0 10px;padding:8px 10px;border:1px dashed var(--line,#dddcd5);
    border-radius:10px;background:var(--surface2)}
  .ts-file-name{color:var(--muted,#73716b);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ts-file-name.picked{color:inherit}
  /* 子节点：共享出去的 auth key / 已接入的节点 */
  .ts-keys{display:flex;flex-direction:column;gap:8px;padding:6px 0 2px}
  .ts-key{display:flex;align-items:center;gap:10px;padding:9px 11px;border:1px solid var(--line,#dddcd5);
    border-radius:10px;background:var(--surface2);flex:none}
  .ts-key-main{flex:1;min-width:0}
  .ts-key-name{font-weight:650;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ts-key-meta{margin-top:2px;color:var(--muted,#73716b);font-size:11.5px;line-height:1.5;overflow-wrap:anywhere}
  .ts-key-value{margin:6px 0 0;padding:7px 9px;border:1px dashed var(--line,#dddcd5);border-radius:9px;
    background:var(--card);color:inherit;font:11.5px ui-monospace,SFMono-Regular,Menlo,monospace;
    overflow-wrap:anywhere;word-break:break-all;user-select:all}
  .ts-key{flex-wrap:wrap}
  .ts-key-actions{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;margin-left:auto}
  .ts-key .ts-btn{flex:none}
  .ts-key form{width:100%}
  .ts-field select{height:36px;padding:0 8px;border:1px solid var(--line,#dddcd5);border-radius:9px;
    background:var(--card);color:inherit;font:13px inherit;outline:none}
  .ts-field select:focus{border-color:var(--accent,#c96442)}
  .ts-field select:disabled{opacity:.55}
  /* plugin manager */
  .ts-plugins{display:flex;flex-direction:column;gap:8px;padding:10px 0 4px}
  .ts-tail{display:flex;align-items:center;gap:8px}
  .ts-tail .ts-hint{margin:0}
  .ts-themes{display:flex;flex-wrap:wrap;gap:8px;padding:10px 0 6px}
  .ts-theme{display:flex;align-items:center;gap:8px;height:34px;padding:0 11px;border:1px solid var(--line,#dddcd5);
    border-radius:10px;background:var(--card);color:var(--ink,#242421);font:12px inherit;cursor:pointer}
  .ts-theme:hover{border-color:var(--line-strong)}
  .ts-theme[aria-pressed="true"]{border-color:var(--accent,#c96442);box-shadow:0 0 0 2px rgba(201,100,66,.18)}
  .ts-swatch{width:16px;height:16px;border-radius:50%;flex:none}
  .ts-plugin{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border:1px solid var(--line,#dddcd5);
    border-radius:12px;background:var(--surface2)}
  .ts-plugin.off{background:var(--surface2)}
  .ts-plugin-main{flex:1;min-width:0}
  .ts-plugin-name{font-weight:650;font-size:13px}
  .ts-plugin-ver{margin-left:6px;color:var(--faint,#a09d95);font:10.5px ui-monospace,SFMono-Regular,Menlo,monospace}
  .ts-plugin .ts-hint{margin:3px 0 0;font-size:11.5px}
  .ts-plugin .ts-state{margin:3px 0 0;min-height:0;font-size:11.5px}
  .ts-switch{flex:none;position:relative;width:42px;height:24px;margin-top:2px;padding:0;border:1px solid var(--line,#dddcd5);
    border-radius:999px;background:var(--surface2);cursor:pointer;transition:background .15s,border-color .15s}
  .ts-switch::after{content:'';position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;
    background:var(--card);box-shadow:0 1px 2px #0002;transition:transform .15s}
  .ts-switch[aria-checked="true"]{background:#4f9a65;border-color:#4f9a65}
  .ts-switch[aria-checked="true"]::after{transform:translateX(18px)}
  .ts-switch:disabled{opacity:.5;cursor:default}
  .ts-actions{display:flex;justify-content:space-between;gap:8px;padding:14px 20px 18px}
  /* fingers need bigger targets than a mouse pointer */
  @media (pointer:coarse){
    .ts-row{padding:13px 0 11px}
    .ts-tabs button{height:32px}
    .ts-btn{height:38px;padding:0 14px;font-size:13px}
    .ts-field input,.ts-field select{height:42px;font-size:16px}
    .ts-key{padding:11px 12px}
    .ts-key .ts-btn{height:34px}
  }
  `;

  const MARKUP = `
  <form class="ts-form" id="ts-form">
    <h2>设置</h2>

    <section class="ts-block">
      <a class="ts-row ts-link" href="/hosts"><span>子节点和服务器管理</span><span class="ts-go" aria-hidden="true"></span></a>
      <a class="ts-row ts-link" href="/shares"><span>分享管理</span><span class="ts-go" aria-hidden="true"></span></a>
    </section>

    <section class="ts-block">
      <button class="ts-row" type="button" id="ts-child-toggle" aria-expanded="false" aria-controls="ts-child-panel">
        <span>子节点：让别人接入</span>
        <span class="ts-tail"><span class="ts-hint" id="ts-child-count"></span><span class="ts-caret" aria-hidden="true"></span></span>
      </button>
      <div class="ts-panel" id="ts-child-panel" hidden>
        <p class="ts-hint">把本机的窗格交给另一个控制台。生成一串 auth key 给它：能做什么、能看哪些窗格、多久失效，都由这里决定，随时可以吊销。
          「发消息」等于把你的键盘交给对方，「文件读写」等于把整台机器的文件给它看和改 —— 只给信得过的控制台。</p>
        <p class="ts-hint">本机允许外部做哪些事 —— 勾上就同时是「允许」和「签给这把 key 的权限」；取消勾选后，
          连已经签出去的 auth key 也立刻失效。「只能看」永远都有。</p>
        <div class="ts-scopes">
          <label><input type="checkbox" data-policy="input"> 发消息 / 按键</label>
          <label><input type="checkbox" data-policy="files"> 文件读写</label>
          <label><input type="checkbox" data-policy="admin"> 管理会话 + 终端</label>
        </div>
        <p class="ts-state" id="ts-child-policy-state" role="status"></p>
        <label class="ts-field">有效期
          <select id="ts-child-ttl" aria-label="auth key 有效期">
            <option value="3600">1 小时</option>
            <option value="86400" selected>1 天</option>
            <option value="604800">7 天</option>
            <option value="2592000">30 天</option>
            <option value="0">永久</option>
          </select>
        </label>
        <label class="ts-field">范围
          <select id="ts-child-range" aria-label="共享哪些窗格">
            <option value="all" selected>全部窗格</option>
            <option value="pick">只给这几个</option>
          </select>
        </label>
        <label class="ts-field" id="ts-child-picks" hidden>窗格编号（逗号分隔，写 #12 或 %12 都行）
          <input id="ts-child-panes" placeholder="#12, #14" autocomplete="off" spellcheck="false">
        </label>
        <div class="ts-btns"><button class="ts-btn primary" type="button" id="ts-child-create">生成 auth key</button></div>
        <p class="ts-state" id="ts-child-state" role="status"></p>
        <div id="ts-child-issued"></div>
        <div class="ts-keys" id="ts-child-keys"></div>
      </div>
    </section>

    <section class="ts-block">
      <button class="ts-row" type="button" id="ts-pw-toggle" aria-expanded="false" aria-controls="ts-pw-panel">
        <span>修改密码</span><span class="ts-caret" aria-hidden="true"></span>
      </button>
      <div class="ts-panel" id="ts-pw-panel" hidden>
        <p class="ts-hint">当前来源：<span id="ts-pw-source">读取中…</span></p>
        <label class="ts-field">当前密码<input id="ts-pw-current" type="password" autocomplete="current-password"></label>
        <label class="ts-field">新密码<input id="ts-pw-new" type="password" autocomplete="new-password" minlength="6" placeholder="至少 6 位"></label>
        <label class="ts-field">确认新密码<input id="ts-pw-confirm" type="password" autocomplete="new-password" minlength="6"></label>
        <p class="ts-state" id="ts-pw-state" role="status"></p>
        <div class="ts-btns"><button class="ts-btn primary" id="ts-pw-submit" type="submit">修改密码</button></div>
      </div>
    </section>

    <section class="ts-block">
      <button class="ts-row" type="button" id="ts-direct-toggle" aria-expanded="false" aria-controls="ts-direct-panel">
        <span>直连地址</span><span class="ts-caret" aria-hidden="true"></span>
      </button>
      <div class="ts-panel" id="ts-direct-panel" hidden>
        <p class="ts-hint" id="ts-direct-hint">同一局域网或 Tailscale 里可以直接连服务器，传输不经过 Cloudflare（没有 100MB / 125 秒限制）。</p>
        <div class="ts-row ts-static">
          <span>文件下载走直连源站</span>
          <span class="ts-tail">
            <button class="ts-switch" type="button" role="switch" id="ts-transfer-direct" aria-checked="true"
              title="下载文件时把字节改走局域网 / Tailscale 源站" aria-label="文件下载走直连源站"></button>
          </span>
        </div>
        <p class="ts-hint" id="ts-transfer-hint">只管本机 / SSH 主机的下载：开着时下载会先探测一次直连地址，
          把字节改走局域网 / Tailscale；关掉就一律走当前页面（通常是 Cloudflare）。
          子节点的 pane 不受这个开关影响 —— 它按子节点自己的线路走（先域名，域名不通再试它的内网地址）。</p>
        <div class="ts-btns"><button class="ts-btn" type="button" id="ts-direct-forget">清除记住的直连源站</button></div>
        <p class="ts-state" id="ts-direct-state" role="status"></p>
        <div id="ts-direct-list"></div>
      </div>
    </section>

    <section class="ts-block">
      <button class="ts-row" type="button" id="ts-io-toggle" aria-expanded="false" aria-controls="ts-io-panel">
        <span>导入 / 导出</span><span class="ts-caret" aria-hidden="true"></span>
      </button>
      <div class="ts-panel" id="ts-io-panel" hidden>
        <div class="ts-tabs" role="group" aria-label="导入或导出">
          <button type="button" data-ts-tab="export" class="on">导出</button>
          <button type="button" data-ts-tab="import">导入</button>
        </div>

        <div id="ts-export">
          <p class="ts-hint">含服务器凭据、分窗布局与预设。先设一个导出密码，文件用 AES-GCM 加密，密码只在浏览器里使用、不会上传。</p>
          <label class="ts-field">导出密码<input id="ts-export-password" type="password" autocomplete="new-password" minlength="6" placeholder="至少 6 位，导入时要用"></label>
          <div class="ts-btns">
            <button class="ts-btn" type="button" data-ts-export="hosts">导出服务器</button>
            <button class="ts-btn" type="button" data-ts-export="grid">导出分窗布局</button>
            <button class="ts-btn" type="button" data-ts-export="all">全部导出</button>
          </div>
          <p class="ts-state" id="ts-export-state" role="status"></p>
        </div>

        <div id="ts-import" hidden>
          <p class="ts-hint">选之前导出的文件并输入当时的密码。已存在的主机（同地址/端口/用户）会跳过，分窗布局直接覆盖本机。</p>
          <div class="ts-file">
            <button class="ts-btn" type="button" id="ts-import-pick">选择文件</button>
            <span class="ts-file-name" id="ts-import-name">未选择文件</span>
          </div>
          <input id="ts-import-file" type="file" accept=".json,application/json" hidden>
          <label class="ts-field">文件密码<input id="ts-import-password" type="password" autocomplete="current-password" placeholder="导出时设置的密码"></label>
          <div class="ts-btns"><button class="ts-btn primary" type="button" id="ts-import-run">导入</button></div>
          <p class="ts-state" id="ts-import-state" role="status"></p>
        </div>
      </div>
    </section>

    <section class="ts-block">
      <button class="ts-row" type="button" id="ts-plugin-toggle" aria-expanded="false" aria-controls="ts-plugin-panel">
        <span>插件</span>
        <span class="ts-tail"><span class="ts-hint" id="ts-plugins-count"></span><span class="ts-caret" aria-hidden="true"></span></span>
      </button>
      <div class="ts-panel" id="ts-plugin-panel" hidden>
        <div class="ts-plugins" id="ts-plugins"><p class="ts-hint">读取中…</p></div>
      </div>
    </section>

    <section class="ts-block">
      <button class="ts-row" type="button" id="ts-prompt-toggle" aria-expanded="false" aria-controls="ts-prompt-panel">
        <span>交互菜单卡片（实验性）</span><span class="ts-caret" aria-hidden="true"></span>
      </button>
      <div class="ts-panel" id="ts-prompt-panel" hidden>
        <p class="ts-hint">agent 在终端里弹出选择时（kimi 的模型选择器、claude 的权限确认），在聊天里显示成可点的卡片。
          实验性：靠读屏识别，可能把屏幕上别的内容误认成菜单，默认关闭。</p>
        <div class="ts-tabs" role="group" aria-label="交互菜单卡片">
          <button type="button" data-ts-prompt="off" class="on">关</button>
          <button type="button" data-ts-prompt="on">开</button>
        </div>
        <p class="ts-state" id="ts-prompt-state" role="status"></p>
      </div>
    </section>

    <section class="ts-block">
      <button class="ts-row" type="button" id="ts-pause-toggle" aria-expanded="false" aria-controls="ts-pause-panel">
        <span>暂停实时刷新按钮（实验性）</span><span class="ts-caret" aria-hidden="true"></span>
      </button>
      <div class="ts-panel" id="ts-pause-panel" hidden>
        <p class="ts-hint">顶栏那个「暂停实时刷新」按钮。默认隐藏：要冻结某个 pane 的画面手动看一会儿时才用得上。</p>
        <div class="ts-tabs" role="group" aria-label="暂停实时刷新按钮">
          <button type="button" data-ts-pause="off" class="on">关</button>
          <button type="button" data-ts-pause="on">开</button>
        </div>
        <p class="ts-state" id="ts-pause-state" role="status"></p>
      </div>
    </section>

    <section class="ts-block">
      <button class="ts-row" type="button" id="ts-theme-toggle" aria-expanded="false" aria-controls="ts-theme-panel">
        <span>主题</span>
        <span class="ts-tail"><span class="ts-hint" id="ts-theme-current"></span><span class="ts-caret" aria-hidden="true"></span></span>
      </button>
      <div class="ts-panel" id="ts-theme-panel" hidden>
        <div class="ts-themes" id="ts-themes"></div>
        <p class="ts-hint">主题记在本浏览器里；插件自己的页面也会跟着变。</p>
      </div>
    </section>

    <section class="ts-block">
      <div class="ts-row ts-static">
        <span>悬停预览</span>
        <span class="ts-tail">
          <select class="ts-select" id="ts-preview-delay" aria-label="侧栏悬停预览延迟">
            <option value="off">关闭</option>
            <option value="1">1 秒</option>
            <option value="2">2 秒</option>
            <option value="3">3 秒</option>
          </select>
        </span>
      </div>
      <p class="ts-hint" id="ts-preview-hint">鼠标在侧栏的服务器或「最近」一行上停多久，才弹出最后一条消息。手机没有悬停，默认关闭。</p>
    </section>

    <section class="ts-block">
      <div class="ts-row ts-static">
        <span>侧栏</span>
        <span class="ts-tail">
          <button class="ts-switch" type="button" role="switch" id="ts-sidebar-memory" aria-checked="false"
            title="按 pane 记住侧栏视图" aria-label="按 pane 记住侧栏视图"></button>
        </span>
      </div>
      <p class="ts-hint" id="ts-sidebar-hint">关闭时每次进控制台都从会话列表开始；开启后每个 pane 记住自己上次是会话列表还是文件面板（含目录）。</p>
    </section>

    <div class="ts-actions">
      <button class="ts-btn danger" type="button" id="ts-logout">退出登录</button>
      <button class="ts-btn" type="button" id="ts-close">关闭</button>
    </div>
  </form>`;

  const EXPORT_FORMAT = 'tmuxweb-config-encrypted';
  const EXPORT_VERSION = 1;
  const EXPORT_ITERATIONS = 250000;
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  let dialog = null;
  let sections = [];
  let mounted = false;

  const $ = id => document.getElementById(id);

  function note(el, text, kind) {
    if (!el) return;
    el.className = 'ts-state' + (kind ? ` ${kind}` : '');
    el.textContent = text;
  }

  function toBase64(bytes) {
    let out = '';
    for (const byte of bytes) out += String.fromCharCode(byte);
    return btoa(out);
  }

  function fromBase64(text) {
    const raw = atob(text);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
    return out;
  }

  async function exportKey(password, salt) {
    const base = await crypto.subtle.importKey('raw', encoder.encode(password), 'PBKDF2', false, ['deriveKey']);
    return crypto.subtle.deriveKey(
      { name: 'PBKDF2', salt, iterations: EXPORT_ITERATIONS, hash: 'SHA-256' },
      base, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
  }

  async function encryptBundle(bundle, password) {
    if (!crypto.subtle) throw new Error('这个页面不是安全上下文（需要 https 或 localhost）');
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const key = await exportKey(password, salt);
    const sealed = await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv, additionalData: encoder.encode(EXPORT_FORMAT) },
      key, encoder.encode(JSON.stringify(bundle)));
    return {
      format: EXPORT_FORMAT, version: EXPORT_VERSION, algorithm: 'AES-GCM',
      kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations: EXPORT_ITERATIONS, salt: toBase64(salt) },
      iv: toBase64(iv), payload: toBase64(new Uint8Array(sealed)),
    };
  }

  async function decryptBundle(envelope, password) {
    if (!envelope || envelope.format !== EXPORT_FORMAT) throw new Error('这不是 TmuxWeb 导出的配置文件');
    if (envelope.version !== EXPORT_VERSION) throw new Error(`不支持的配置版本 ${envelope.version}`);
    if (!crypto.subtle) throw new Error('这个页面不是安全上下文（需要 https 或 localhost）');
    const key = await exportKey(password, fromBase64(envelope.kdf.salt));
    let plain;
    try {
      plain = await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: fromBase64(envelope.iv), additionalData: encoder.encode(EXPORT_FORMAT) },
        key, fromBase64(envelope.payload));
    } catch {
      throw new Error('密码不对，或者文件被改过');
    }
    try {
      return JSON.parse(decoder.decode(plain));
    } catch {
      throw new Error('文件内容不是合法 JSON');
    }
  }

  function download(name, data) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  }

  function readStore(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch {
      return fallback;
    }
  }

  async function api(url, options = {}) {
    const response = await fetch(url, options);
    if (response.status === 401) {
      location.href = '/login';
      throw new Error('登录已失效');
    }
    if (!response.ok) {
      let detail = {};
      try { detail = await response.json(); } catch { /* plain error page */ }
      throw new Error(detail.detail || `HTTP ${response.status}`);
    }
    return response.json();
  }

  async function exportConfig(which) {
    const out = $('ts-export-state');
    const password = $('ts-export-password').value;
    if (password.length < 6) {
      note(out, '导出密码至少 6 位', 'bad');
      $('ts-export-password').focus();
      return;
    }
    note(out, '正在收集并加密…');
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    try {
      const bundle = { app: 'TmuxWeb', exported_at: new Date().toISOString(), scope: which };
      let hostCount = 0;
      if (which !== 'grid') {
        const data = await api('/api/settings/export');
        bundle.hosts = data.hosts || [];
        hostCount = bundle.hosts.length;
      }
      if (which !== 'hosts') {
        bundle.grid = readStore('tmux-grid-v1', null);
        bundle.grid_presets = readStore('tmux-grid-presets-v1', null);
      }
      download(`tmuxweb-${which}-${stamp}.tmuxweb.json`, await encryptBundle(bundle, password));
      const panes = (bundle.grid && bundle.grid.panes || []).length;
      note(out, `已加密导出 ${which === 'grid' ? '分窗布局' : which === 'hosts' ? '服务器配置' : '全部'}`
        + `${hostCount ? `（${hostCount} 台主机）` : ''}${panes ? `（${panes} 个窗口）` : ''}，导入需要同一个密码`, 'good');
    } catch (error) {
      note(out, `导出失败：${error.message}`, 'bad');
    }
  }

  async function importConfig() {
    const out = $('ts-import-state');
    const file = $('ts-import-file').files[0];
    const password = $('ts-import-password').value;
    if (!file) { note(out, '先选择要导入的文件', 'bad'); return; }
    if (!password) { note(out, '输入导出时设置的密码', 'bad'); $('ts-import-password').focus(); return; }
    note(out, '正在解密…');
    try {
      let envelope;
      try {
        envelope = JSON.parse(await file.text());
      } catch {
        throw new Error('文件不是合法 JSON');
      }
      const bundle = await decryptBundle(envelope, password);
      const lines = [];
      const created = [];
      if (Array.isArray(bundle.hosts) && bundle.hosts.length) {
        const existing = await api('/api/remote-hosts');
        const known = new Set((existing.hosts || []).map(host => `${host.hostname}|${host.port || 22}|${host.username}`));
        const failures = [];
        let skipped = 0;
        for (const host of bundle.hosts) {
          if (!host || !host.hostname || !host.username) { failures.push('有一条主机记录缺地址或用户名'); continue; }
          const key = `${host.hostname}|${host.port || 22}|${host.username}`;
          if (known.has(key)) { skipped += 1; continue; }
          const body = {
            name: (host.name || host.hostname).slice(0, 64), hostname: host.hostname, port: host.port || 22,
            username: host.username, auth_type: host.auth_type || 'agent', credential: host.credential || '',
            host_key: host.host_key || '', fingerprint: host.fingerprint || '',
            jump_spec: host.jump_spec || '', verify: false,
          };
          try {
            const saved = await api('/api/remote-hosts', {
              method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
            created.push({ host: saved, source: host });
            known.add(key);
          } catch (error) {
            failures.push(`${host.name || host.hostname}: ${error.message}`);
          }
        }
        // jump chains are saved by id, which changes on import: relink by name
        const byName = new Map(created.map(entry => [entry.source.name || entry.source.hostname, entry.host.id]));
        for (const entry of created) {
          const target = entry.source.jump_name ? byName.get(entry.source.jump_name) : '';
          if (!target || target === entry.host.id) continue;
          try {
            await api(`/api/remote-hosts/${encodeURIComponent(entry.host.id)}`, {
              method: 'PUT', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ jump_host_id: target, verify: false }),
            });
          } catch (error) {
            failures.push(`${entry.source.name}: 跳板链没接上（${error.message}）`);
          }
        }
        lines.push(`主机：新增 ${created.length}，跳过 ${skipped}${failures.length ? `，问题 ${failures.length}` : ''}`);
        if (failures.length) out.title = failures.join('\n');
      }
      if (bundle.grid && bundle.grid.panes) {
        localStorage.setItem('tmux-grid-v1', JSON.stringify(bundle.grid));
        lines.push('分窗布局已覆盖');
      }
      if (bundle.grid_presets) {
        localStorage.setItem('tmux-grid-presets-v1', JSON.stringify(bundle.grid_presets));
        lines.push('分窗预设已覆盖');
      }
      note(out, (lines.length ? lines.join('；') : '文件里没有可导入的内容'), 'good');
    } catch (error) {
      note(out, `导入失败：${error.message}`, 'bad');
    }
  }

  function toggleSection(toggleId, force) {
    const pair = sections.find(([id]) => id === toggleId);
    if (!pair) return;
    const panel = $(pair[1]);
    const open = force === undefined ? panel.hidden : force;
    for (const [otherToggle, otherPanel] of sections) {
      const mine = otherToggle === toggleId;
      $(otherPanel).hidden = !(mine && open);
      $(otherToggle).setAttribute('aria-expanded', String(mine && open));
    }
  }

  function selectTab(name) {
    for (const button of dialog.querySelectorAll('[data-ts-tab]')) {
      button.classList.toggle('on', button.dataset.tsTab === name);
    }
    $('ts-export').hidden = name !== 'export';
    $('ts-import').hidden = name !== 'import';
  }

  // ---------------------------------------------------------------- sidebar memory
  const SIDEBAR_PREF_KEY = 'tmux-sidebar-memory';

  function paintSidebarPref(enabled) {
    const toggle = $('ts-sidebar-memory');
    if (!toggle) return;
    toggle.setAttribute('aria-checked', enabled ? 'true' : 'false');
    toggle.title = enabled ? '按 pane 记住（点击关闭）' : '不记住（点击开启）';
    toggle.setAttribute('aria-label', toggle.title);
    const hint = $('ts-sidebar-hint');
    if (hint) {
      hint.textContent = enabled
        ? '已开启：每个 pane 记住自己上次是会话列表还是文件面板（含目录），重新打开控制台也恢复。'
        : '关闭时每次进控制台都从会话列表开始；打开后每个 pane 记住自己上次的视图。';
    }
  }

  const PREVIEW_KEY = 'tmux-preview-delay';
  const PREVIEW_VALUES = new Set(['off', '1', '2', '3']);

  const PAUSE_KEY = 'tmux-pause-button';
  function paintPauseButton(on) {
    for (const button of document.querySelectorAll('[data-ts-pause]')) {
      button.classList.toggle('on', (button.dataset.tsPause === 'on') === on);
    }
    try { localStorage.setItem(PAUSE_KEY, on ? 'on' : 'off'); } catch {}
    window.dispatchEvent(new CustomEvent('tmuxweb:pause-button', {detail: on}));
  }

  async function loadPauseButtonPref() {
    let on = false;
    try { on = localStorage.getItem(PAUSE_KEY) === 'on'; } catch {}
    paintPauseButton(on);
    try {
      const data = await api('/api/prefs');
      const server = (data.prefs || {}).pause_button;
      if (server === 'on' || server === 'off') paintPauseButton(server === 'on');
    } catch { /* 离线：用本地镜像 */ }
  }

  async function savePauseButtonPref(on) {
    paintPauseButton(on);
    try {
      await api('/api/prefs', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pause_button: on ? 'on' : 'off'}),
      });
      note($('ts-pause-state'), on ? '已打开：顶栏出现暂停按钮' : '已关闭：按钮不再显示', on ? 'ok' : '');
    } catch {
      note($('ts-pause-state'), '保存到账号失败，已记在本浏览器', 'bad');
    }
  }

  const PROMPT_KEY = 'tmux-prompt-card';
  function paintPromptCard(on) {
    for (const button of document.querySelectorAll('[data-ts-prompt]')) {
      button.classList.toggle('on', (button.dataset.tsPrompt === 'on') === on);
    }
    try { localStorage.setItem(PROMPT_KEY, on ? 'on' : 'off'); } catch {}
    // 控制台据此决定要不要轮询 /prompt
    window.dispatchEvent(new CustomEvent('tmuxweb:prompt-card', {detail: on}));
  }

  async function loadPromptCardPref() {
    let on = false;
    try { on = localStorage.getItem(PROMPT_KEY) === 'on'; } catch {}
    paintPromptCard(on);
    try {
      const data = await api('/api/prefs');
      const server = (data.prefs || {}).prompt_card;
      if (server === 'on' || server === 'off') paintPromptCard(server === 'on');
    } catch { /* 离线：用本地镜像 */ }
  }

  async function savePromptCardPref(on) {
    paintPromptCard(on);
    try {
      await api('/api/prefs', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({prompt_card: on ? 'on' : 'off'}),
      });
      note($('ts-prompt-state'), on ? '已打开（立即生效）' : '已关闭（立即生效）', on ? 'ok' : '');
    } catch {
      note($('ts-prompt-state'), '保存到账号失败，已记在本浏览器', 'bad');
    }
  }

  function paintPreviewDelay(value) {
    const select = $('ts-preview-delay');
    if (select) select.value = PREVIEW_VALUES.has(value) ? value : '2';
  }

  const TRANSFER_KEY = 'tmuxweb.transfer.direct';

  function paintTransferDirect(on) {
    const toggle = $('ts-transfer-direct');
    if (!toggle) return;
    toggle.setAttribute('aria-checked', on ? 'true' : 'false');
    const hint = $('ts-transfer-hint');
    if (hint) {
      hint.textContent = on
        ? '开着：下载会先探测一次直连地址，然后把字节改走它（浏览器 → 本机直连，不经 CF）。'
        : '关掉：下载一律走当前页面（通常是 Cloudflare），由 CF 转发到本机。';
    }
  }

  async function loadTransferPref() {
    let on = true;
    try {
      const route = localStorage.getItem(ROUTE_KEY);
      on = route ? route !== 'cf' : localStorage.getItem(TRANSFER_KEY) !== 'off';
    } catch {}
    paintTransferDirect(on);
    try {
      const data = await api('/api/prefs');
      const stored = (data.prefs || {}).transfer;
      if (stored === 'on' || stored === 'off') {
        on = stored === 'on';
        try {
          localStorage.setItem(TRANSFER_KEY, on ? 'on' : 'off');
          if (!localStorage.getItem(ROUTE_KEY)) {
            localStorage.setItem(ROUTE_KEY, on ? 'ts' : 'cf');
          }
        } catch {}
        paintTransferDirect(on);
      }
    } catch { /* 离线：用本地镜像 */ }
  }

  const ROUTE_KEY = 'tmuxweb.transfer.route';

  async function toggleTransferDirect() {
    const toggle = $('ts-transfer-direct');
    if (!toggle) return;
    const next = toggle.getAttribute('aria-checked') !== 'true';
    paintTransferDirect(next);
    try { localStorage.setItem(TRANSFER_KEY, next ? 'on' : 'off'); } catch {}
    // 顶栏的「线路」按钮读的是这一条：关掉 = 走当前页面（CF），打开就回到 TS
    try { localStorage.setItem(ROUTE_KEY, next ? 'ts' : 'cf'); } catch {}
    try {
      await api('/api/prefs', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ transfer: next ? 'on' : 'off' }),
      });
    } catch {
      note($('ts-direct-state'), '保存到账号失败，已记在本浏览器', 'bad');
    }
  }

  async function loadPreviewPref() {
    let stored = '2';
    try { stored = localStorage.getItem(PREVIEW_KEY) || '2'; } catch {}
    paintPreviewDelay(stored);
    try {
      const data = await api('/api/prefs');
      const server = (data.prefs || {}).preview;
      if (PREVIEW_VALUES.has(server)) {
        try { localStorage.setItem(PREVIEW_KEY, server); } catch {}
        paintPreviewDelay(server);
      }
    } catch { /* offline: the local mirror is what the pages read */ }
  }

  async function savePreviewDelay(value) {
    const next = PREVIEW_VALUES.has(value) ? value : '2';
    paintPreviewDelay(next);
    try { localStorage.setItem(PREVIEW_KEY, next); } catch {}
    try {
      await api('/api/prefs', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preview: next }),
      });
    } catch {
      note($('ts-preview-hint'), '保存到账号失败，已记在本浏览器', 'bad');
    }
  }

  async function loadSidebarPref() {
    let enabled = false;
    try { enabled = localStorage.getItem(SIDEBAR_PREF_KEY) === 'pane'; } catch {}
    paintSidebarPref(enabled);
    try {
      const data = await api('/api/prefs');
      const stored = (data.prefs || {}).sidebar;
      if (stored === 'pane' || stored === 'none') {
        enabled = stored === 'pane';
        try { localStorage.setItem(SIDEBAR_PREF_KEY, stored); } catch {}
        paintSidebarPref(enabled);
      }
    } catch { /* offline: the local mirror is what the console reads */ }
  }

  async function toggleSidebarMemory() {
    const toggle = $('ts-sidebar-memory');
    if (!toggle) return;
    const next = toggle.getAttribute('aria-checked') !== 'true';
    paintSidebarPref(next);
    try { localStorage.setItem(SIDEBAR_PREF_KEY, next ? 'pane' : 'none'); } catch {}
    try {
      await api('/api/prefs', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sidebar: next ? 'pane' : 'none' }),
      });
    } catch {
      note($('ts-sidebar-hint'), '保存到账号失败，已记在本浏览器', 'bad');
    }
  }

  // ---------------------------------------------------------------- themes
  function renderThemes() {
    const box = $('ts-themes');
    if (!box || !window.TmuxTheme) return;
    const current = window.TmuxTheme.get();
    const themes = window.TmuxTheme.list();
    box.innerHTML = themes.map(theme => `
      <button class="ts-theme" type="button" data-theme="${theme.name}" aria-pressed="${theme.name === current}"
        title="${theme.hint}">
        <span class="ts-swatch" style="background:${theme.page};box-shadow:inset 0 0 0 2px ${theme.accent}"></span>
        ${theme.label}
      </button>`).join('');
    const label = $('ts-theme-current');
    if (label) label.textContent = (themes.find(theme => theme.name === current) || {}).label || '';
  }

  function chooseTheme(name) {
    if (!window.TmuxTheme) return;
    window.TmuxTheme.set(name);
    renderThemes();
  }

  // ---------------------------------------------------------------- plugins
  const esc = value => String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function renderPlugins(list) {
    const box = $('ts-plugins');
    if (!box) return;
    const count = $('ts-plugins-count');
    if (count) {
      const on = list.filter(row => row.enabled).length;
      count.textContent = list.length ? `${on} / ${list.length} 已启用` : '没有安装插件';
    }
    if (!list.length) {
      box.innerHTML = '<p class="ts-hint">把插件放到 plugins/&lt;名字&gt;/ 里，重启后就能在这里开关。</p>';
      return;
    }
    box.innerHTML = list.map(row => `
      <div class="ts-plugin${row.enabled ? '' : ' off'}" data-plugin="${esc(row.name)}">
        <div class="ts-plugin-main">
          <span class="ts-plugin-name">${esc(row.name)}</span><span class="ts-plugin-ver">${esc(row.version)}</span>
          ${row.description ? `<p class="ts-hint">${esc(row.description)}</p>` : ''}
          ${row.error ? `<p class="ts-state bad">${esc(row.error)}</p>` : ''}
        </div>
        <button class="ts-switch" type="button" role="switch" data-toggle="${esc(row.name)}"
          aria-checked="${row.enabled ? 'true' : 'false'}"
          title="${row.enabled ? '停用' : '启用'}" aria-label="${row.enabled ? '停用' : '启用'} ${esc(row.name)}"
          ${row.error && !row.loaded ? 'disabled' : ''}></button>
      </div>`).join('');
  }

  async function loadPlugins() {
    const box = $('ts-plugins');
    if (!box) return;
    try {
      const data = await api('/api/plugins');
      renderPlugins(data.plugins || []);
    } catch (error) {
      box.innerHTML = `<p class="ts-state bad">读取插件失败：${esc(error.message)}</p>`;
    }
  }

  async function togglePlugin(name, button) {
    const wanted = button.getAttribute('aria-checked') !== 'true';
    button.disabled = true;
    try {
      await api(`/api/plugins/${encodeURIComponent(name)}/${wanted ? 'enable' : 'disable'}`, { method: 'POST' });
      await loadPlugins();
      // let the sidebar redraw its plugin buttons without a page reload
      document.dispatchEvent(new CustomEvent('tmuxweb:plugins-changed'));
    } catch (error) {
      button.disabled = false;
      const row = button.closest('.ts-plugin');
      const note = row && row.querySelector('.ts-state');
      if (note) { note.textContent = error.message; note.className = 'ts-state bad'; }
      else if (row) row.insertAdjacentHTML('beforeend', `<p class="ts-state bad">${esc(error.message)}</p>`);
    }
  }

  async function loadDirectEntries() {
    // 换了网络（离开 tailnet / 隧道关了）时，浏览器里记住的直连源站会让"点下载没反应"，
    // 这里给一颗手动清除的按钮，不用等人跑去清 localStorage
    const forgetButton = $('ts-direct-forget');
    if (forgetButton) {
      forgetButton.onclick = () => {
        try { if (window.TmuxTransfer && TmuxTransfer.forget) TmuxTransfer.forget(); } catch {}
        try { localStorage.removeItem('tmuxweb.transfer.origin'); } catch {}
        note($('ts-direct-state'), '已清除，下次下载重新探测', 'ok');
      };
    }
    const list = $('ts-direct-list');
    const hint = $('ts-direct-hint');
    if (!list) return;
    list.innerHTML = '<p class="ts-hint">读取中…</p>';
    try {
      const data = await api('/api/system/addresses');
      const entries = (data.addresses || []).filter(item => item.kind !== 'public');
      if (!entries.length) {
        list.innerHTML = '<p class="ts-hint">这台机器没有可用的局域网 / Tailscale 地址。</p>';
        return;
      }
      const currentHost = data.host || location.host;
      list.innerHTML = entries.map(item => {
        const here = new URL(item.url).host === currentHost;
        return `<div class="ts-direct-row"><span class="ts-direct-kind">${esc(item.kind === 'tailnet' ? 'TS' : 'LAN')}</span>`
          + `<code>${esc(item.url)}</code>`
          + (here ? '<span class="ts-state ok">当前</span>' : '')
          + `<button class="ts-btn ts-copy" type="button" data-copy="${esc(item.url)}">复制</button></div>`;
      }).join('');
      const cf = (data.addresses || []).find(item => item.kind === 'public');
      hint.textContent = cf
        ? `当前页面走 ${cf.url}（Cloudflare）。下面这些地址不经 CF，局域网/tailnet 里用它们传文件最快。`
        : '局域网/Tailscale 直连地址：传输不经过 Cloudflare。';
    } catch (error) {
      list.innerHTML = `<p class="ts-state bad">读取失败：${esc(error.message)}</p>`;
    }
  }

  // ---- 子节点：本级签发的 auth key，以及接入的子节点 ----------------------
  const SCOPE_NAMES = { view: '只能看', input: '发按键', files: '文件读写', admin: '管理会话' };

  function fmtWhen(stamp) {
    if (stamp === null || stamp === undefined) return '永久';
    const when = new Date(stamp * 1000);
    const pad = number => String(number).padStart(2, '0');
    return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`
      + ` ${pad(when.getHours())}:${pad(when.getMinutes())}`;
  }

  // 勾选 = 权限：view 永远有，其余看勾没勾
  function policyScopes() {
    const scopes = ['view'];
    for (const name of ['input', 'files', 'admin']) {
      if (policyState[name]) scopes.push(name);
    }
    return scopes;
  }

  function paneList(text) {
    return (text || '').split(/[\s,，、]+/).filter(Boolean)
      .map(item => item.replace(/^[#%]/, '%'));
  }

  function scopeText(scopes) {
    return (scopes || []).map(scope => SCOPE_NAMES[scope] || scope).join(' + ');
  }

  // 正在编辑的那把 key（点「编辑」后原地展开表单）
  let editingKey = null;
  let policyState = { input: false, files: false, admin: false };

  const SCOPE_LABELS = { input: '发消息 / 按键', files: '文件读写', admin: '管理会话 + 终端' };

  function scopeChecksHtml(current) {
    const granted = new Set(current || ['view']);
    return Object.entries(SCOPE_LABELS).map(([scope, label]) => {
      const blocked = !policyState[scope];
      return `<label><input type="checkbox" data-key-scope="${scope}"`
        + `${granted.has(scope) ? ' checked' : ''}${blocked ? ' disabled' : ''}> ${label}`
        + `${blocked ? '（本机没开放）' : ''}</label>`;
    }).join('');
  }

  function keyEditorHtml(key) {
    const picked = key.panes !== 'all';
    const paneText = Array.isArray(key.panes) ? key.panes.join(', ') : '';
    return `<div class="ts-key"><div class="ts-key-main">`
      + `<div class="ts-key-name">编辑 ${esc(key.label || '未命名')}</div>`
      + `<div class="ts-scopes" id="ts-key-scopes">${scopeChecksHtml(key.scopes)}</div>`
      + `<label class="ts-field">有效期<select id="ts-key-ttl">`
      + '<option value="">保持不变（' + esc(fmtWhen(key.expires_at)) + '）</option>'
      + '<option value="3600">1 小时</option><option value="86400">1 天</option>'
      + '<option value="604800">7 天</option><option value="2592000">30 天</option>'
      + '<option value="0">永久</option></select></label>'
      + `<label class="ts-field">范围<select id="ts-key-range">`
      + `<option value="all"${picked ? '' : ' selected'}>全部窗格</option>`
      + `<option value="pick"${picked ? ' selected' : ''}>只给这几个</option></select></label>`
      + `<label class="ts-field" id="ts-key-picks"${picked ? '' : ' hidden'}>编号（逗号分隔）`
      + `<input id="ts-key-panes" value="${esc(paneText)}" autocomplete="off"></label>`
      + `<div class="ts-btns"><button class="ts-btn primary" type="button" data-save="${esc(key.id)}">保存</button>`
      + `<button class="ts-btn" type="button" data-cancel="1">取消</button></div>`
      + '</div></div>';
  }

  async function loadChildKeys() {
    const box = $('ts-child-keys');
    const counter = $('ts-child-count');
    try {
      const data = await api('/api/fed/tokens');
      const keys = data.tokens || [];
      counter.textContent = keys.filter(key => !key.revoked).length
        ? `${keys.filter(key => !key.revoked).length} 个有效` : '';
      box.innerHTML = keys.length ? keys.map(key => {
        if (editingKey === key.id) return keyEditorHtml(key);
        const range = key.panes === 'all' ? '全部窗格' : `指定 ${(key.panes || []).join(' ')}`;
        const when = key.revoked ? '已吊销' : `到期 ${fmtWhen(key.expires_at)}`;
        const actions = key.revoked
          ? `<button class="ts-btn danger" type="button" data-forget="${esc(key.id)}">删除</button>`
          : `<button class="ts-btn" type="button" data-edit="${esc(key.id)}">编辑</button>`
            + `<button class="ts-btn danger" type="button" data-revoke="${esc(key.id)}">吊销</button>`
            + `<button class="ts-btn danger" type="button" data-forget="${esc(key.id)}">删除</button>`;
        return '<div class="ts-key"><div class="ts-key-main">'
          + `<div class="ts-key-name">${esc(key.label || '未命名')}</div>`
          + `<div class="ts-key-meta">${esc(scopeText(key.scopes))} · ${esc(range)} · ${esc(when)}</div></div>`
          + `<div class="ts-key-actions">${actions}</div></div>`;
      }).join('') : '<p class="ts-hint">还没生成过 auth key。</p>';
    } catch (error) {
      counter.textContent = '';
      box.innerHTML = `<p class="ts-state bad">读取失败：${esc(error.message)}</p>`;
    }
  }

  async function saveKeyEdits(id) {
    const out = $('ts-child-state');
    const scopes = ['view', ...[...document.querySelectorAll('[data-key-scope]')]
      .filter(box => box.checked).map(box => box.dataset.keyScope)];
    const ttl = $('ts-key-ttl').value;
    const picked = $('ts-key-range').value === 'pick';
    const panes = picked ? paneList($('ts-key-panes').value) : 'all';
    if (picked && !panes.length) { note(out, '写一下要共享哪几个窗格', 'bad'); return; }
    const body = { scopes, panes };
    if (ttl !== '') body.ttl = Number(ttl);
    note(out, '正在保存…');
    try {
      await api(`/api/fed/tokens/${id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      editingKey = null;
      note(out, '已更新（立即生效）', 'good');
      loadChildKeys();
    } catch (error) {
      note(out, error.message, 'bad');
    }
  }

  async function createChildKey() {
    const out = $('ts-child-state');
    const issued = $('ts-child-issued');
    const scopes = policyScopes();
    const ttl = Number($('ts-child-ttl').value);
    const picked = $('ts-child-range').value === 'pick';
    const panes = picked ? paneList($('ts-child-panes').value) : 'all';
    if (picked && !panes.length) { note(out, '写一下要共享哪几个窗格，比如 #12', 'bad'); return; }
    $('ts-child-create').disabled = true;
    note(out, '正在生成…');
    try {
      const made = await api('/api/fed/tokens', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: '控制台', scopes, panes, ttl }),
      });
      // 把"本机是怎么被访问到的"接在 key 后面，父节点只填名字 + 这一串就够
      const full = `${made.token}@${location.origin}`;
      issued.innerHTML = '<div class="ts-key"><div class="ts-key-main">'
        + '<div class="ts-key-name">新 auth key（只显示这一次）</div>'
        + `<div class="ts-key-meta">到期 ${esc(fmtWhen(made.expires_at))} · 复制到对方的「添加子节点」</div>`
        + `<div class="ts-key-value">${esc(full)}</div></div>`
        + `<button class="ts-btn" type="button" data-copy="${esc(full)}">复制</button></div>`;
      note(out, '已生成。关掉面板就看不到了，需要时重新生成或吊销。', 'good');
      loadChildKeys();
    } catch (error) {
      note(out, error.message, 'bad');
    } finally {
      $('ts-child-create').disabled = false;
    }
  }

  async function loadChildPolicy() {
    try {
      const data = await api('/api/fed/policy');
      policyState = data.allow;
      for (const box of document.querySelectorAll('[data-policy]')) {
        box.checked = !!data.allow[box.dataset.policy];
      }
    } catch (error) {
      note($('ts-child-policy-state'), `读取失败：${error.message}`, 'bad');
    }
  }

  async function saveChildPolicy() {
    const out = $('ts-child-policy-state');
    const flags = {};
    for (const box of document.querySelectorAll('[data-policy]')) flags[box.dataset.policy] = box.checked;
    note(out, '正在保存…');
    try {
      const data = await api('/api/fed/policy', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(flags),
      });
      // 记住新政策：紧接着「生成 auth key」就是按这三个勾给权限的
      policyState = data.allow;
      note(out, '已保存（立刻生效，已签出的 key 也受这个限制）', 'good');
    } catch (error) {
      note(out, error.message, 'bad');
      loadChildPolicy();
    }
  }

  async function open() {
    if (!dialog) return;
    sections.forEach(([id]) => toggleSection(id, false));
    selectTab('export');
    $('ts-form').reset();
    for (const id of ['ts-pw-state', 'ts-export-state', 'ts-import-state']) note($(id), '');
    $('ts-import-name').textContent = '未选择文件';
    $('ts-import-name').classList.remove('picked');
    $('ts-pw-source').textContent = '读取中…';
    dialog.showModal();
    renderThemes();
    loadSidebarPref();
    loadPreviewPref();
    loadPlugins();          // fills the row's counter; the list itself stays collapsed
    loadDirectEntries();
    loadTransferPref();
    loadChildKeys();
    loadChildPolicy();
    // Chrome/Firefox auto-focus the first control, Safari draws its blue ring on it: park the focus on
    // the dialog instead, so opening 设置 never comes with a highlighted row
    for (const element of dialog.querySelectorAll('.ts-row, .ts-btn, a, button, input')) {
      if (document.activeElement === element) element.blur();
    }
    dialog.tabIndex = -1;
    dialog.focus({ preventScroll: true });
    try {
      const info = await api('/api/settings');
      $('ts-pw-source').textContent = info.password_source === 'file'
        ? '已自定义（哈希保存在 passwords.json，可用这里再次修改）'
        : '环境变量 TMUX_WEB_PASSWORD（改一次后即保存到 passwords.json）';
    } catch (error) {
      $('ts-pw-source').textContent = `读取失败：${error.message}`;
    }
  }

  function mount() {
    if (mounted) return true;
    const buttons = [...document.querySelectorAll('#settings-open, [data-settings-open]')];
    if (!buttons.length) return false;

    if (!document.getElementById('ts-style')) {
      const style = document.createElement('style');
      style.id = 'ts-style';
      style.textContent = STYLE;
      document.head.appendChild(style);
    }
    if (!document.getElementById('ts-dialog')) {
      const shell = document.createElement('dialog');
      shell.id = 'ts-dialog';
      shell.className = 'ts-dialog';
      shell.setAttribute('aria-labelledby', 'ts-title');
      shell.innerHTML = MARKUP;
      document.body.appendChild(shell);
    }
    dialog = $('ts-dialog');
    dialog.querySelector('h2').id = 'ts-title';

    sections = [['ts-child-toggle', 'ts-child-panel'],
                ['ts-pw-toggle', 'ts-pw-panel'], ['ts-direct-toggle', 'ts-direct-panel'],
                ['ts-io-toggle', 'ts-io-panel'],
                ['ts-plugin-toggle', 'ts-plugin-panel'], ['ts-theme-toggle', 'ts-theme-panel'],
                // 这两个（交互菜单、暂停按钮）以前没登记进 sections，点了展不开
                ['ts-prompt-toggle', 'ts-prompt-panel'], ['ts-pause-toggle', 'ts-pause-panel']];
    for (const [toggleId] of sections) $(toggleId).onclick = () => {
      toggleSection(toggleId);
      if (toggleId === 'ts-plugin-toggle' && !$('ts-plugin-panel').hidden) loadPlugins();
    };
    for (const button of dialog.querySelectorAll('[data-ts-tab]')) {
      button.onclick = () => selectTab(button.dataset.tsTab);
    }
    for (const button of buttons) button.onclick = () => open();
    $('ts-close').onclick = () => dialog.close();
    dialog.addEventListener('click', event => {
      const button = event.target.closest('[data-copy]');
      if (!button) return;
      event.preventDefault();
      const text = button.dataset.copy;
      const done = () => { button.textContent = '已复制'; setTimeout(() => { button.textContent = '复制'; }, 1200); };
      const legacyCopy = () => {
        const area = document.createElement('textarea');
        area.value = text; document.body.appendChild(area); area.select();
        try { document.execCommand('copy'); done(); } catch (error) { /* ignore */ }
        area.remove();
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(legacyCopy);
      } else {
        legacyCopy();
      }
    });
    $('ts-child-range').addEventListener('change', event => {
      $('ts-child-picks').hidden = event.target.value !== 'pick';
    });
    $('ts-child-create').addEventListener('click', () => createChildKey());
    for (const box of document.querySelectorAll('[data-policy]')) {
      box.addEventListener('change', () => saveChildPolicy());
    }
    $('ts-child-keys').addEventListener('click', async event => {
      const out = $('ts-child-state');
      const edit = event.target.closest('[data-edit]');
      if (edit) {
        editingKey = edit.dataset.edit;
        loadChildKeys();
        return;
      }
      const cancel = event.target.closest('[data-cancel]');
      if (cancel) {
        editingKey = null;
        loadChildKeys();
        return;
      }
      const save = event.target.closest('[data-save]');
      if (save) { saveKeyEdits(save.dataset.save); return; }
      const revoke = event.target.closest('[data-revoke]');
      if (revoke) {
        revoke.disabled = true;
        try {
          await api(`/api/fed/tokens/${revoke.dataset.revoke}`, { method: 'DELETE' });
          note(out, '已吊销，那台控制台下次请求就会被拒', 'good');
          loadChildKeys();
        } catch (error) {
          revoke.disabled = false;
          note(out, error.message, 'bad');
        }
        return;
      }
      const forget = event.target.closest('[data-forget]');
      if (forget) {
        if (!confirm('删除这条 auth key 记录？（对方会立刻失效，记录也不再显示）')) return;
        forget.disabled = true;
        try {
          await api(`/api/fed/tokens/${forget.dataset.forget}?purge=1`, { method: 'DELETE' });
          note(out, '已删除', 'good');
          loadChildKeys();
        } catch (error) {
          forget.disabled = false;
          note(out, error.message, 'bad');
        }
      }
    });
    $('ts-child-keys').addEventListener('change', event => {
      if (event.target.id === 'ts-key-range') {
        $('ts-key-picks').hidden = event.target.value !== 'pick';
      }
    });
    $('ts-transfer-direct').addEventListener('click', () => toggleTransferDirect());
    $('ts-sidebar-memory').addEventListener('click', () => toggleSidebarMemory());
    $('ts-preview-delay').addEventListener('change', event => savePreviewDelay(event.target.value));
  for (const button of document.querySelectorAll('[data-ts-prompt]')) {
    button.addEventListener('click', () => savePromptCardPref(button.dataset.tsPrompt === 'on'));
  }
  loadPromptCardPref();
  for (const button of document.querySelectorAll('[data-ts-pause]')) {
    button.addEventListener('click', () => savePauseButtonPref(button.dataset.tsPause === 'on'));
  }
  loadPauseButtonPref();
    $('ts-themes').addEventListener('click', event => {
      const button = event.target.closest('[data-theme]');
      if (button) chooseTheme(button.dataset.theme);
    });
    $('ts-plugins').addEventListener('click', event => {
      const button = event.target.closest('[data-toggle]');
      if (button) togglePlugin(button.dataset.toggle, button);
    });
    $('ts-logout').onclick = async () => {
      try { await fetch('/logout', { method: 'POST' }); } catch { /* the redirect decides */ }
      location.href = '/login';
    };
    for (const button of dialog.querySelectorAll('[data-ts-export]')) {
      button.onclick = () => exportConfig(button.dataset.tsExport);
    }
    $('ts-import-pick').onclick = () => $('ts-import-file').click();
    $('ts-import-file').onchange = () => {
      const file = $('ts-import-file').files[0];
      $('ts-import-name').textContent = file
        ? `${file.name}（${file.size < 1024 ? `${file.size} B` : `${(file.size / 1024).toFixed(1)} KB`}）`
        : '未选择文件';
      $('ts-import-name').classList.toggle('picked', !!file);
    };
    $('ts-import-run').onclick = () => importConfig();
    $('ts-form').addEventListener('submit', async event => {
      event.preventDefault();
      const out = $('ts-pw-state');
      const current = $('ts-pw-current').value;
      const next = $('ts-pw-new').value;
      const again = $('ts-pw-confirm').value;
      if (next !== again) { note(out, '两次输入的新密码不一致', 'bad'); return; }
      $('ts-pw-submit').disabled = true;
      note(out, '正在修改…');
      try {
        await api('/api/settings/password', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ current, next }),
        });
        note(out, '密码已更新，其他设备需要重新登录；这个浏览器保持登录', 'good');
        $('ts-pw-current').value = '';
        $('ts-pw-new').value = '';
        $('ts-pw-confirm').value = '';
        $('ts-pw-source').textContent = '已自定义（哈希保存在 passwords.json，可用这里再次修改）';
      } catch (error) {
        note(out, error.message, 'bad');
      } finally {
        $('ts-pw-submit').disabled = false;
      }
    });

    mounted = true;
    return true;
  }

  window.TmuxSettings = { mount, open };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => mount(), { once: true });
  } else {
    mount();
  }
})();
