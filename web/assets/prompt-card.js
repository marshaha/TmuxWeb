/* Native waits and manual inspection share this card. The server owns request verification. */
(function () {
  const $ = id => document.getElementById('prompt-card' + (id ? '-' + id : ''));
  let paneNow = () => null;
  let current = null;
  let cursor = 0;
  let retiredCodex = null;
  let epoch = 0;
  let reading = null;
  let sending = false;
  let followTimer = 0;
  let manual = false;
  let automatic = true;
  let suppressed = '';
  let bound = false;
  let currentScreen = [];
  let sharedStatus = () => null;
  let notify;
  let onNavigation;
  let lastStatus;
  let timeoutMs = 300000;
  let reshowCooldownMs = 60000;
  let hideTimer = 0;
  let tickTimer = 0;
  let reshowAt = 0;

  function actionable(data) {
    return data?.verified === true && ['hermes_clarify', 'codex_command_approval'].includes(data.source)
      && data.read_only === false && !!data.revision;
  }

  function codexCard(data) {
    return data?.source === 'codex_command_approval' && !!data.revision;
  }

  function observeCodexScreen(pane, screen) {
    if (pane !== paneNow()) return;
    if (screen && retiredCodex?.screen === screen) retiredCodex = null;
    if (!codexCard(current) || !current.screen_revision || current.screen_revision === screen) return;
    retiredCodex = {screen: current.screen_revision, revision: current.revision};
    manual = false;
    hide();
  }

  function cancelTimeout() {
    clearTimeout(hideTimer);
    clearInterval(tickTimer);
    if ($('timeout')) $('timeout').textContent = '';
  }

  function hide() {
    cancelTimeout();
    $().hidden = true;
    current = null;
    onNavigation?.();
  }

  function paintCursor() {
    [...$('options').children].forEach((button, index) => {
      const selected = (actionable(current) || codexCard(current)) && index === cursor;
      button.classList.toggle('on', selected);
      button.setAttribute('aria-selected', String(selected));
    });
    onNavigation?.();
  }

  function render(data, screen = []) {
    cancelTimeout();
    current = data;
    currentScreen = screen;
    cursor = data.selected || 0;
    $('title').textContent = data.title || (data.kind === 'confirm' ? '需要确认' : '等待选择');
    const hint = $('hint');
    hint.textContent = data.hint || '';
    hint.hidden = !hint.textContent;

    const list = $('options');
    list.innerHTML = '';
    list.setAttribute('aria-multiselectable', String(data.kind === 'multi'));
    data.options.forEach((option, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'prompt-option';
      button.setAttribute('role', 'option');
      button.dataset.index = String(index);
      button.disabled = !actionable(data) && !codexCard(data);
      const label = document.createElement('span');
      label.className = 'prompt-label';
      label.textContent = (option.number ? option.number + '. ' : '') + (option.label || '');
      const text = document.createElement('span');
      text.className = 'prompt-option-text';
      text.appendChild(label);
      if (option.description) {
        const description = document.createElement('span');
        description.className = 'prompt-desc';
        description.textContent = option.description;
        text.appendChild(description);
      }
      button.appendChild(text);
      if (codexCard(data) && (!actionable(data) || option.native_only)) {
        const link = document.createElement('span');
        link.className = 'prompt-meta';
        link.textContent = '实时确认 ›';
        button.appendChild(link);
      }
      if (option.meta) {
        const meta = document.createElement('span');
        meta.className = 'prompt-meta';
        meta.textContent = option.meta;
        button.appendChild(meta);
      }
      button.addEventListener('focus', () => { cursor = index; paintCursor(); });
      button.addEventListener('click', () => { if (data === current) submit({index}); });
      list.appendChild(button);
    });

    const nav = $('nav');
    nav.hidden = !actionable(data) || !data.options.length;
    for (const button of nav.children) {
      button.hidden = ['Tab', 'BTab'].includes(button.dataset.nav) && !(data.question_count > 1);
    }
    $('state').textContent = '';
    $('raw').textContent = screen.join('\n');
    $('raw-wrap').hidden = !screen.length;
    $('raw-wrap').open = data.kind === 'screen';
    $().hidden = false;
    paintCursor();
  }

  function showStatus(found) {
    if (!automatic || !paneNow() || sending || reading || followTimer || document.hidden) return;
    const previous = lastStatus;
    lastStatus = found?.revision || '';
    if (!found || !lastStatus) {
      if (!manual || (previous && current?.revision === previous)) {
        manual = false;
        hide();
      }
      suppressed = '';
      return;
    }
    if (manual && current && (lastStatus === previous || current.revision === lastStatus)) return;
    if (codexCard(found) && found.revision === retiredCodex?.revision) return;
    if (current?.revision === lastStatus) return;
    if (suppressed === lastStatus && Date.now() < reshowAt) return;
    manual = false;
    render(found);
    const expires = Date.now() + timeoutMs;
    const tick = () => {
      const seconds = Math.max(0, Math.ceil((expires - Date.now()) / 1000));
      $('timeout').textContent = Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0') + ' 后自动收起';
    };
    tick();
    tickTimer = setInterval(tick, 1000);
    hideTimer = setTimeout(() => {
      suppressed = found.revision;
      reshowAt = Date.now() + reshowCooldownMs;
      hide();
    }, timeoutMs);
  }

  async function inspect(getter = paneNow, previousRevision = '', attempt = 0) {
    paneNow = getter;
    const pane = paneNow();
    if (!pane || sending) return;
    clearTimeout(followTimer);
    followTimer = 0;
    reading?.abort();
    const request = new AbortController();
    reading = request;
    const version = epoch;
    const active = () => version === epoch && paneNow() === pane && reading === request;
    manual = true;
    try {
      const fresh = previousRevision ? '&fresh=true' : '';
      const response = await fetch(`/api/panes/${encodeURIComponent(pane)}/prompt?inspect=true${fresh}`,
        {credentials: 'same-origin', signal: request.signal});
      const data = await response.json();
      if (!active()) return;
      if (!response.ok) throw new Error(data.detail || 'HTTP ' + response.status);
      if (previousRevision && (!data.prompt || data.prompt.revision === previousRevision)) {
        if (attempt < 2) followTimer = setTimeout(() => inspect(getter, previousRevision, attempt + 1), 650);
        else if (current?.kind === 'navigating') {
          render({kind: 'screen', title: '尚未读到下一题', hint: '可在实时终端查看下一题。', options: []}, data.screen);
        } else manual = false;
        return;
      }
      render(data.prompt || {kind: 'screen', title: '暂无已确认的交互',
        hint: '可查看屏幕原文，或点「实时作答」操作终端。', options: []}, data.screen);
      if (actionable(current)) $('options').children[cursor]?.focus();
    } catch (error) {
      if (active()) render({kind: 'screen', title: '读取交互失败', hint: error.message, options: []});
    } finally {
      if (reading === request) reading = null;
    }
  }

  async function submit(action) {
    const pane = paneNow(), saved = current, screen = currentScreen, version = epoch;
    const viaTerminal = codexCard(saved) && Number.isInteger(action.index) &&
      (!actionable(saved) || saved.options[action.index]?.native_only);
    if (!pane || (!actionable(saved) && !viaTerminal) || sending) return;
    sending = true;
    reading?.abort();
    reading = null;
    suppressed = saved.revision;
    reshowAt = Infinity;
    if (action.key && saved.question_count > 1) {
      render({kind: 'navigating', title: '正在切换题目…', options: []});
      $().focus({preventScroll: true});
    } else hide();
    try {
      if (viaTerminal) {
        const response = await fetch(`/api/panes/${encodeURIComponent(pane)}/prompt?inspect=true&fresh=true`,
          {credentials: 'same-origin', cache: 'no-store'});
        const data = await response.json();
        if (epoch !== version || paneNow() !== pane) return;
        if (!response.ok) throw new Error(data.detail || 'HTTP ' + response.status);
        if (data.prompt?.revision !== saved.revision) {
          throw new Error('这条命令审批已结束或已更换。');
        }
        const label = saved.options[action.index].label;
        document.getElementById('chat-reply-panel')?.dispatchEvent(new CustomEvent('open-reply', {detail: {label}}));
        return;
      }
      const response = await fetch(`/api/panes/${encodeURIComponent(pane)}/prompt/choose`, {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...action, revision: saved.revision}),
      });
      const data = await response.json();
      if (epoch !== version || paneNow() !== pane) return;
      if (!response.ok) throw new Error(data.detail || 'HTTP ' + response.status);
      manual = false;
      if (codexCard(saved)) notify?.('已发送「' + saved.options[action.index].label + '」');
      if (saved.options[action.index]?.opens_input) {
        document.getElementById('chat-reply-panel')?.dispatchEvent(new Event('open-reply'));
      } else if (saved.question_count > 1) {
        followTimer = setTimeout(() => inspect(paneNow, saved.revision), 200);
      }
    } catch (error) {
      if (epoch === version && paneNow() === pane) {
        manual = true;
        render({kind: 'screen', title: '请重新查看当前交互', hint: error.message, options: []}, screen);
      }
    } finally {
      if (epoch === version) {
        sending = false;
        onNavigation?.();
      }
    }
  }

  function navigate(key, focus = true) {
    if ((!actionable(current) && !codexCard(current)) || sending || !current.options.length) return;
    if (key === 'BTab' || key === 'Tab') {
      if (current.question_count > 1) submit({key});
    } else if (key === 'ArrowDown' || key === 'ArrowUp') {
      cursor = (cursor + (key === 'ArrowDown' ? 1 : -1) + current.options.length) % current.options.length;
      paintCursor();
      if (focus) {
        $('options').children[cursor]?.focus({preventScroll: true});
        $('options').children[cursor]?.scrollIntoView?.({block: 'nearest'});
      }
    } else if (key === 'Enter') {
      submit({index: cursor});
    }
  }

  function navigation() {
    return actionable(current) && !sending ? {prompt: current, cursor} : null;
  }

  function navigateLive(key, revision) {
    if (actionable(current) && current.revision === revision) navigate(key, false);
  }

  function bind() {
    if (bound) return;
    bound = true;
    $('close').onclick = () => {
      suppressed = current?.revision || lastStatus;
      reshowAt = Infinity;
      refresh(false);
    };
    $('terminal').onclick = () => document.getElementById('chat-reply-panel')?.dispatchEvent(new Event('open-reply'));
    $('nav').addEventListener('click', event => {
      const button = event.target.closest('button[data-nav]');
      if (button && actionable(current)) navigate(button.dataset.nav);
    });
    $().addEventListener('keydown', event => {
      if ((!actionable(current) && !codexCard(current)) || event.target?.closest?.('.prompt-foot,.prompt-head,.prompt-nav,summary')) return;
      let key = event.key;
      if (current.question_count > 1 && ['Tab', 'ArrowLeft', 'ArrowRight'].includes(key)) {
        key = key === 'ArrowLeft' || event.shiftKey ? 'BTab' : 'Tab';
      } else if (!['ArrowDown', 'ArrowUp', 'Enter', 'Escape'].includes(key)) return;
      event.preventDefault();
      if (key === 'Escape') $('close').focus();
      else if (!event.repeat || key.startsWith('Arrow')) navigate(key);
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) showStatus(sharedStatus()); });
  }

  function refresh(clear = true) {
    epoch++;
    reading?.abort();
    reading = null;
    clearTimeout(followTimer);
    followTimer = 0;
    manual = sending = false;
    hide();
    if (clear) {
      retiredCodex = null;
      suppressed = '';
      lastStatus = '';
      showStatus(sharedStatus());
    }
  }

  function start(getter, status, onNotice, onNavigationChange) {
    notify = onNotice;
    onNavigation = onNavigationChange;
    paneNow = getter;
    sharedStatus = status;
    bind();
    automatic = document.getElementById('interaction-placement')?.value !== 'manual';
    showStatus(status());
  }

  function closeInspection() {
    if (!manual || sending || followTimer) return;
    refresh(false);
    showStatus(sharedStatus());
  }

  function setAutomatic(on) {
    if (automatic === on) return;
    automatic = on;
    if (on) showStatus(sharedStatus());
    else if (!manual && !sending && !followTimer) hide();
  }

  function configure(options = {}) {
    if (Number.isFinite(options.timeoutMs) && options.timeoutMs > 0) timeoutMs = options.timeoutMs;
    if (Number.isFinite(options.reshowCooldownMs) && options.reshowCooldownMs >= 0) reshowCooldownMs = options.reshowCooldownMs;
    return {timeoutMs, reshowCooldownMs};
  }

  window.TmuxPrompt = {start, navigation, navigateLive, inspect, closeInspection, refresh, showStatus, observeCodexScreen, configure, setAutomatic};
})();
