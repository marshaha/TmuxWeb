/* 「等你回答」卡片：agent 真的在等你选，才在控制台里把菜单搬成按钮。
 *
 * 和实验性的 prompt-card.js 的区别就在这里：那张卡直接读 /prompt（纯抓屏
 * 解析，设置里默认关闭，因为读屏识别偶尔会误判），这张卡只读
 * /api/panes/status 里已经过三道关的 needs_input —— turn 已结束（session
 * 文件说了算）+ 屏幕连续两轮解析一致 + 菜单确实存在。解析 alone 永远点不
 * 亮这张卡（门在 needs_input.py，不在这里）。
 *
 * 点一个选项，就是把服务端随 prompt 下发的 keys（choice_keys 算好的方向键 /
 * 数字 / y-n + Enter）一条一条 POST /api/panes/{id}/key 发进去 —— 不另起
 * 发送路径。答完之后卡片先自己收起来：sweep 一分钟才重扫一轮，/status 里
 * 的旧菜单还会挂一会儿，照着旧签名继续弹等于让人对着已经答过的题再答一次。
 *
 * 点下的一刻卡片就收起来（乐观关闭），按键在后台发：手机上走 Cloudflare，
 * 逐个 POST 要好几秒，卡片一直杵在“已发送（等屏幕刷新…）”上看着像卡住了。
 * 发失败了卡片会恢复出来、显示原因，可以再点一次；恢复时签名清掉，poll
 * 才不会以为这道题已经答过、压住重弹。
 *
 * 超时（对标 Open WebUI ask_user 的 timeout_ms，默认 120 秒自动取消）：
 * 卡片亮起 timeoutMs 后自动收起，不作答、不发任何键；收起后进入
 * reshowCooldownMs 冷却，冷却期内就算门控仍成立也不重弹，冷却一过、
 * 下一次 poll 看到门控仍成立就重新出现 —— 超时只是把卡片叠起来，
 * 不是把这道题永久关掉。默认值 5 分钟不是照搬 120 秒：TmuxWeb 的等待
 * 没有服务端过期、门控本身就要约 1–2 分钟（两轮 sweep）才亮灯，120 秒
 * 会比亮灯还快；而用户常常是过几分钟才回来看手机，卡片要等得到他。
 * 「等你」角标（pane 列表里那个）不受超时影响，一直亮到等待结束。
 * 超时时长可配：TmuxNeedsYou.configure({timeoutMs, reshowCooldownMs})。
 *
 * 选项描述与首选项（同样对标 ask_user：每题 2–3 个选项，label + description，
 * 首选项标 Recommended）：解析器从菜单里抠出的 description 显示为 label
 * 下的描述行，首选项（options[0]，带 recommended）打「推荐」标。
 * description 与 recommended 只是展示，不进签名 —— 描述换行重排不该
 * 让稳定计数重来（门在 needs_input.prompt_signature，不在这里）。
 *
 *   TmuxNeedsYou.start(() => state.selected)
 *   TmuxNeedsYou.stop()
 *   TmuxNeedsYou.configure({timeoutMs: 60000})
 */
(function () {
  const POLL_MS = 10000;
  const CARD = 'needs-you-card';
  const DEFAULT_TIMEOUT_MS = 5 * 60 * 1000;
  const DEFAULT_RESHOW_COOLDOWN_MS = 60 * 1000;

  let getPane = null;
  let timer = null;
  let busy = false;
  let prompt = null;
  let cursor = 0;
  let hiddenByUser = false;
  let answeredSignature = '';   // 答过的菜单签名：sweep 更新之前别再弹
  let followUpTimer = 0;
  let timeoutMs = DEFAULT_TIMEOUT_MS;
  let reshowCooldownMs = DEFAULT_RESHOW_COOLDOWN_MS;
  let hideTimer = 0;            // 超时收起
  let tickTimer = 0;            // 倒计时每秒刷新
  let shownAt = 0;
  let timedOut = null;          // {signature, at}：冷却期内别重弹

  const $ = id => document.getElementById(CARD + '-' + id);

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  }

  function paneNow() {
    try { return getPane ? getPane() : ''; } catch (_error) { return ''; }
  }

  function card() { return document.getElementById(CARD); }

  function signatureOf(found) {
    if (!found) return '';
    // description 与 recommended 不进签名：它们只是展示，描述换行重排
    // 不该让「同一道题」的判断翻脸（门在 needs_input.prompt_signature）
    return JSON.stringify([
      found.kind || '', found.title || '',
      (found.options || []).map(option => [option.label, option.meta, option.number]),
      (found.toggles || []).map(toggle => [toggle.label, toggle.active]),
      found.selected,
    ]);
  }

  // 需要先置顶的 pane 在前，其余保持原顺序（Array.prototype.sort 是稳定的）。
  function sortNeedsFirst(panes, needsIds) {
    const wanted = needsIds instanceof Set ? needsIds : new Set(needsIds || []);
    return [...(panes || [])].sort((a, b) =>
      (wanted.has(b.id) ? 1 : 0) - (wanted.has(a.id) ? 1 : 0));
  }

  function hide() {
    disarmTimeout();
    const node = card();
    if (node) node.hidden = true;
    const timeout = $('timeout');
    if (timeout) timeout.textContent = '';
    prompt = null;
    cursor = 0;
  }

  function configure(options) {
    if (options && Number.isFinite(options.timeoutMs) && options.timeoutMs > 0) {
      timeoutMs = Math.floor(options.timeoutMs);
    }
    if (options && Number.isFinite(options.reshowCooldownMs) && options.reshowCooldownMs >= 0) {
      reshowCooldownMs = Math.floor(options.reshowCooldownMs);
    }
    return settings();
  }

  function settings() {
    return {timeoutMs, reshowCooldownMs};
  }

  function disarmTimeout() {
    if (hideTimer) clearTimeout(hideTimer);
    if (tickTimer) clearInterval(tickTimer);
    hideTimer = 0;
    tickTimer = 0;
  }

  function updateCountdown() {
    const node = $('timeout');
    if (!node || !prompt) return;
    const left = timeoutMs - (Date.now() - shownAt);
    if (left <= 0) { node.textContent = ''; return; }
    const seconds = Math.ceil(left / 1000);
    node.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} 后自动收起`;
  }

  function armTimeout() {
    disarmTimeout();
    shownAt = Date.now();
    hideTimer = setTimeout(onTimeout, timeoutMs);
    tickTimer = setInterval(updateCountdown, 1000);
    updateCountdown();
  }

  // 超时只收卡片：不作答、不发任何键。记下签名 + 时间，冷却期内不重弹，
  // 冷却一过、门控仍成立，下一次 poll 就重新出现。
  function onTimeout() {
    if (!prompt) return;
    timedOut = {signature: signatureOf(prompt), at: Date.now()};
    hide();
    setState('');
  }

  function setState(text, bad) {
    const node = $('state');
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('bad', !!bad);
  }

  function paintCursor() {
    const list = $('options');
    if (!list || !prompt || !prompt.options || !prompt.options.length) return;
    [...list.children].forEach((node, index) => {
      node.classList.toggle('on', index === cursor);
      node.setAttribute('aria-selected', index === cursor ? 'true' : 'false');
    });
  }

  function render(data) {
    const node = card();
    if (!node) return;
    prompt = data;
    cursor = Number.isInteger(data.selected) ? Math.min(data.selected, data.options.length - 1) : 0;

    $('title').textContent = data.title || (data.kind === 'confirm' ? '需要确认' : '等你回答');
    const hint = $('hint');
    hint.textContent = data.hint || (data.more ? `屏幕外还有 ${data.more} 项` : '');
    hint.hidden = !hint.textContent;

    const list = $('options');
    list.innerHTML = '';
    (data.options || []).forEach((option, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'prompt-option';
      button.setAttribute('role', 'option');
      button.dataset.index = String(index);
      const text = document.createElement('span');
      text.className = 'prompt-option-text';
      const label = document.createElement('span');
      label.className = 'prompt-label';
      label.textContent = (option.number ? option.number + '. ' : '') + (option.label || '');
      if (option.recommended) {
        const rec = document.createElement('span');
        rec.className = 'prompt-rec';
        rec.textContent = '推荐';
        label.appendChild(rec);
      }
      text.appendChild(label);
      if (option.description) {
        const desc = document.createElement('span');
        desc.className = 'prompt-desc';
        desc.textContent = option.description;
        text.appendChild(desc);
      }
      button.appendChild(text);
      if (option.meta) {
        const meta = document.createElement('span');
        meta.className = 'prompt-meta';
        meta.textContent = option.meta;
        button.appendChild(meta);
      }
      // 没有 keys 的选项不给点：发不出去的按钮比报错的按钮更诚实。
      if (!option.keys || !option.keys.length) button.disabled = true;
      button.addEventListener('click', () => chooseByIndex(index));
      list.appendChild(button);
    });

    const toggles = $('toggles');
    toggles.innerHTML = '';
    (data.toggles || []).forEach((toggle, toggleIndex) => {
      const row = document.createElement('div');
      row.className = 'prompt-toggle';
      const label = document.createElement('span');
      label.className = 'prompt-toggle-label';
      label.textContent = toggle.label || '开关';
      row.appendChild(label);
      const group = document.createElement('div');
      group.className = 'prompt-toggle-group';
      (toggle.options || []).forEach((option, valueIndex) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'prompt-toggle-btn' + (valueIndex === toggle.active ? ' on' : '');
        button.textContent = option;
        button.setAttribute('aria-pressed', valueIndex === toggle.active ? 'true' : 'false');
        button.addEventListener('click', () => chooseToggle(toggleIndex, valueIndex));
        group.appendChild(button);
      });
      row.appendChild(group);
      if (toggle.hint) {
        const hint = document.createElement('span');
        hint.className = 'prompt-toggle-hint';
        hint.textContent = toggle.hint;
        row.appendChild(hint);
      }
      toggles.appendChild(row);
    });
    toggles.hidden = !(data.toggles && data.toggles.length);

    node.hidden = false;
    hiddenByUser = false;
    // 同一张菜单的未过门版本（实验性 prompt 卡）正在显示的话，让它让位：
    // 两张卡是同一道题，留一张就够了，留过门的那张。
    const promptCard = document.getElementById('prompt-card');
    if (promptCard) promptCard.hidden = true;
    paintCursor();
    armTimeout();
  }

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  async function sendKeys(keys) {
    const pane = paneNow();
    if (!pane) throw new Error('先选择一个 pane');
    for (const key of keys) {
      const response = await fetch(`/api/panes/${encodeURIComponent(pane)}/key`, {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key}),
      });
      if (response.status === 401) { location = '/login'; throw new Error('登录失效'); }
      if (!response.ok) {
        let detail = {};
        try { detail = await response.json(); } catch (_error) {}
        throw new Error(detail.detail || `HTTP ${response.status}`);
      }
      await sleep(60);
    }
  }

  async function chooseByIndex(index) {
    if (!prompt || !prompt.options || !prompt.options[index]) return;
    const option = prompt.options[index];
    if (!option.keys || !option.keys.length) { setState('这个选项没有可发的按键', true); return; }
    if (busy) { setState('上一条还在发送，稍等一下…'); return; }
    busy = true;
    // 乐观关闭：先记签名、再收卡、清掉状态文字，按键放后台发。
    const saved = prompt;
    answeredSignature = signatureOf(prompt);
    hide();
    setState('');
    try {
      await sendKeys(option.keys);
      setState('');
      clearTimeout(followUpTimer);
      followUpTimer = setTimeout(poll, 1500);   // 下一轮 sweep 前先看一眼
    } catch (error) {
      // 没送到：把卡片恢复出来，让用户能重试；签名清掉，poll 才不会以为
      // 这道题已经答过、压住重弹。恢复用点之前那份 prompt（hide() 会置空）。
      answeredSignature = '';
      render(saved);
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      busy = false;
    }
  }

  async function chooseToggle(toggleIndex, valueIndex) {
    if (!prompt || !prompt.toggles || !prompt.toggles[toggleIndex]) return;
    const toggle = prompt.toggles[toggleIndex];
    if (toggle.active === valueIndex) { setState('已经是这个值了'); return; }
    const keys = (toggle.keys_by_value || [])[valueIndex] || [];
    if (!keys.length) { setState('这个开关值没有可发的按键', true); return; }
    if (busy) { setState('上一条还在发送，稍等一下…'); return; }
    busy = true;
    const saved = prompt;
    answeredSignature = signatureOf(prompt);
    hide();
    setState('');
    try {
      await sendKeys(keys);
      setState('');
      clearTimeout(followUpTimer);
      followUpTimer = setTimeout(poll, 1500);
    } catch (error) {
      answeredSignature = '';
      render(saved);
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      busy = false;
    }
  }

  async function chooseByKey(key) {
    if (busy) { setState('上一条还在发送，稍等一下…'); return; }
    busy = true;
    const saved = prompt;
    answeredSignature = signatureOf(prompt);
    hide();
    setState('');
    try {
      await sendKeys([key]);
      setState('');
      clearTimeout(followUpTimer);
      followUpTimer = setTimeout(poll, 1500);
    } catch (error) {
      answeredSignature = '';
      if (saved) render(saved);
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      busy = false;
    }
  }

  async function poll() {
    if (busy || document.hidden) return;
    const pane = paneNow();
    if (!pane) { timedOut = null; if (prompt) hide(); return; }
    try {
      const response = await fetch('/api/panes/status', {credentials: 'same-origin'});
      if (!response.ok) return;
      const data = await response.json();
      const entry = (data.panes || []).find(item => item.id === pane);
      if (!entry || !entry.needs_input || !entry.prompt) {
        answeredSignature = '';
        timedOut = null;
        if (prompt) hide();
        return;
      }
      const signature = signatureOf(entry.prompt);
      if (signature === answeredSignature) return;   // 答过的题，sweep 更新前别再弹
      if (hiddenByUser && prompt && signature === signatureOf(prompt)) return;
      if (prompt && signature === signatureOf(prompt)) return;   // 同一道题，别重绘
      if (timedOut) {
        if (signature === timedOut.signature) {
          // 超时收起后、冷却期内：门控仍成立也不重弹；冷却一过再出现
          if (Date.now() - timedOut.at < reshowCooldownMs) return;
        }
        timedOut = null;
      }
      render(entry.prompt);
    } catch (_error) {
      // 网络抖动：保持现状，下一轮再说
    }
  }

  function bindKeyboard() {
    const node = card();
    if (!node) return;
    node.addEventListener('keydown', event => {
      if (!prompt) return;
      const hasOptions = !!(prompt.options && prompt.options.length);
      if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && hasOptions) {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        cursor = (cursor + step + prompt.options.length) % prompt.options.length;
        paintCursor();
        const target = document.querySelectorAll('#' + CARD + '-options .prompt-option')[cursor];
        if (target) target.focus();
      } else if (event.key === 'Enter' && hasOptions) {
        event.preventDefault();
        chooseByIndex(cursor);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        chooseByKey('Escape');
      }
    });
  }

  function start(paneGetter) {
    getPane = paneGetter;
    const node = card();
    if (!node) return;
    hiddenByUser = false;
    const close = $('close');
    if (close) close.onclick = () => {
      hiddenByUser = true;
      disarmTimeout();   // 手动收起是用户自己的决定：超时别再把它变成
      // 一次「超时收起」（否则冷却一过卡片会自己再弹出来）
      const box = card();
      if (box) box.hidden = true;
    };
    const escButton = $('esc');
    if (escButton) escButton.onclick = () => chooseByKey('Escape');
    bindKeyboard();
    if (timer) clearInterval(timer);
    timer = setInterval(poll, POLL_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    poll();
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    timedOut = null;
    hide();
  }

  function refresh() {
    timedOut = null;   // 换 pane 了：旧 pane 的冷却不该压住新 pane 的卡片
    hide();
    poll();
  }

  window.TmuxNeedsYou = {start, stop, refresh, poll, sortNeedsFirst, signatureOf, configure, settings};
})();
