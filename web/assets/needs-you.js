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
 *   TmuxNeedsYou.start(() => state.selected)
 *   TmuxNeedsYou.stop()
 */
(function () {
  const POLL_MS = 10000;
  const CARD = 'needs-you-card';

  let getPane = null;
  let timer = null;
  let busy = false;
  let prompt = null;
  let cursor = 0;
  let hiddenByUser = false;
  let answeredSignature = '';   // 答过的菜单签名：sweep 更新之前别再弹
  let followUpTimer = 0;

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
    const node = card();
    if (node) node.hidden = true;
    prompt = null;
    cursor = 0;
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
      const label = document.createElement('span');
      label.className = 'prompt-label';
      label.textContent = (option.number ? option.number + '. ' : '') + (option.label || '');
      button.appendChild(label);
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
    cursor = index;
    paintCursor();
    setState('已发送：' + (option.label || '') + '（等屏幕刷新…）', false);
    try {
      await sendKeys(option.keys);
      answeredSignature = signatureOf(prompt);
      hide();
      setState('');
      clearTimeout(followUpTimer);
      followUpTimer = setTimeout(poll, 1500);   // 下一轮 sweep 前先看一眼
    } catch (error) {
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
    setState('已发送（等屏幕刷新…）', false);
    try {
      await sendKeys(keys);
      answeredSignature = signatureOf(prompt);
      hide();
      setState('');
      clearTimeout(followUpTimer);
      followUpTimer = setTimeout(poll, 1500);
    } catch (error) {
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      busy = false;
    }
  }

  async function chooseByKey(key) {
    if (busy) { setState('上一条还在发送，稍等一下…'); return; }
    busy = true;
    try {
      await sendKeys([key]);
      answeredSignature = signatureOf(prompt);
      hide();
      setState('');
      clearTimeout(followUpTimer);
      followUpTimer = setTimeout(poll, 1500);
    } catch (error) {
      setState('没送到，再试一次：' + error.message, true);
    } finally {
      busy = false;
    }
  }

  async function poll() {
    if (busy || document.hidden) return;
    const pane = paneNow();
    if (!pane) { if (prompt) hide(); return; }
    try {
      const response = await fetch('/api/panes/status', {credentials: 'same-origin'});
      if (!response.ok) return;
      const data = await response.json();
      const entry = (data.panes || []).find(item => item.id === pane);
      if (!entry || !entry.needs_input || !entry.prompt) {
        answeredSignature = '';
        if (prompt) hide();
        return;
      }
      const signature = signatureOf(entry.prompt);
      if (signature === answeredSignature) return;   // 答过的题，sweep 更新前别再弹
      if (hiddenByUser && prompt && signature === signatureOf(prompt)) return;
      if (prompt && signature === signatureOf(prompt)) return;   // 同一道题，别重绘
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
    hide();
  }

  function refresh() {
    hide();
    poll();
  }

  window.TmuxNeedsYou = {start, stop, refresh, poll, sortNeedsFirst, signatureOf};
})();
