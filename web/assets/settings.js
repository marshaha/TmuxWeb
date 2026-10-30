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
  .ts-dialog{width:min(calc(100vw - 24px),560px);max-width:560px;max-height:86dvh;overflow:auto;padding:0;
    border:1px solid var(--line,#dddcd5);border-radius:18px;background:#fff;color:var(--ink,#242421);
    box-shadow:0 24px 80px #0003;font:14px/1.5 system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif}
  .ts-dialog::backdrop{background:rgba(32,31,28,.28)}
  .ts-form{padding:0 0 4px}
  .ts-form h2{margin:0;padding:18px 20px 8px;font:500 18px/1.3 Georgia,serif}
  .ts-block{padding:0 20px}
  .ts-row{display:flex;width:100%;align-items:center;justify-content:space-between;padding:10px 0 8px;
    border:0;border-bottom:1px solid var(--line,#dddcd5);background:none;color:inherit;font:600 13px/1.4 inherit;cursor:pointer}
  .ts-row:hover{color:var(--accent,#c96442)}
  .ts-caret{width:7px;height:7px;border-right:1.6px solid currentColor;border-bottom:1.6px solid currentColor;
    transform:rotate(45deg);transition:transform .15s}
  .ts-row[aria-expanded="true"] .ts-caret{transform:rotate(-135deg)}
  .ts-panel{padding:8px 0 4px}
  .ts-panel[hidden]{display:none}
  .ts-hint{margin:0 0 10px;color:var(--muted,#73716b);font-size:12px;line-height:1.6}
  .ts-field{display:flex;flex-direction:column;gap:5px;margin-bottom:10px;font-size:12px;color:var(--muted,#73716b)}
  .ts-field input{height:36px;padding:0 10px;border:1px solid var(--line,#dddcd5);border-radius:9px;background:#fff;
    color:inherit;font:13px inherit;outline:none}
  .ts-field input:focus{border-color:var(--accent,#c96442)}
  .ts-state{margin:6px 0 0;min-height:16px;color:var(--muted,#73716b);font-size:12px}
  .ts-state.good{color:var(--green,#4f9a65)}
  .ts-state.bad{color:var(--danger,#b34b43)}
  .ts-tabs{display:inline-flex;gap:2px;padding:2px;margin-bottom:10px;border:1px solid var(--line,#dddcd5);
    border-radius:9px;background:#f6f5f1}
  .ts-tabs button{height:26px;padding:0 12px;border:0;border-radius:7px;background:transparent;
    color:var(--muted,#73716b);font:12px inherit;cursor:pointer}
  .ts-tabs button.on{background:#292825;color:#fff}
  .ts-btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:2px}
  .ts-btn{height:30px;padding:0 12px;border:1px solid var(--line,#dddcd5);border-radius:9px;background:#fff;
    color:inherit;font:12px inherit;cursor:pointer}
  .ts-btn:hover{border-color:#c8c5bc;background:#f7f6f2}
  .ts-btn.primary{border-color:#292825;background:#292825;color:#fff}
  .ts-btn.primary:hover{background:#3a3934}
  .ts-btn.danger{color:var(--danger,#b34b43);border-color:#eed6cf}
  .ts-btn.danger:hover{background:#fbf1ee}
  .ts-file{display:flex;align-items:center;gap:10px;margin:0 0 10px;padding:8px 10px;border:1px dashed var(--line,#dddcd5);
    border-radius:10px;background:#faf9f6}
  .ts-file-name{color:var(--muted,#73716b);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ts-file-name.picked{color:inherit}
  .ts-actions{display:flex;justify-content:space-between;gap:8px;padding:14px 20px 18px}
  `;

  const MARKUP = `
  <form class="ts-form" id="ts-form">
    <h2>设置</h2>

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

    sections = [['ts-pw-toggle', 'ts-pw-panel'], ['ts-io-toggle', 'ts-io-panel']];
    for (const [toggleId] of sections) $(toggleId).onclick = () => toggleSection(toggleId);
    for (const button of dialog.querySelectorAll('[data-ts-tab]')) {
      button.onclick = () => selectTab(button.dataset.tsTab);
    }
    for (const button of buttons) button.onclick = () => open();
    $('ts-close').onclick = () => dialog.close();
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
