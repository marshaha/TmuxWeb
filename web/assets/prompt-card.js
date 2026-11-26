/* Native waits and manual inspection share this card. The server owns request verification. */
(function () {
  const $ = id => document.getElementById('prompt-card' + (id ? '-' + id : ''));
  let paneNow = () => null;
  let current = null;
  let cursor = 0;
  let retiredCodex = null;
  const cursorMessage = document.getElementById('cursor-chat');
  let screenNotice = null, observedScreen = null, hookChecked = false;
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
  let openInlineTerminal;
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

  function observeCodexScreen(pane, screen, notice = null, checked = false) {
    if (pane !== paneNow()) return;
    const changed = checked !== hookChecked || observedScreen === null || screenNotice?.revision !== notice?.revision || screenNotice?.selected !== notice?.selected;
    hookChecked = checked;
    screenNotice = notice;
    observedScreen = screen ?? null;
    if (screen && retiredCodex?.screen === screen) retiredCodex = null;
    if (screen != null && codexCard(current) && current.screen_revision && current.screen_revision !== screen) {
      retiredCodex = {screen: current.screen_revision, revision: current.revision};
      manual = false;
      hide();
    }
    if (changed) showStatus(sharedStatus());
  }

  function cancelTimeout() {
    clearTimeout(hideTimer);
    clearInterval(tickTimer);
    if ($('timeout')) $('timeout').textContent = '';
  }

  function hide() {
    cancelTimeout();
    $().hidden = true;
    if (cursorMessage) cursorMessage.hidden = true;
    current = null;
    onNavigation?.();
  }

  function focusedOption() {
    const row = $('options').children[cursor];
    return row?.querySelector?.('input[type="radio"]') || row;
  }

  function paintCursor() {
    [...$('options').children].forEach((button, index) => {
      const selected = (actionable(current) || codexCard(current)) && index === cursor;
      const radio = button.querySelector?.('input[type="radio"]');
      if (radio) radio.checked = selected;
      else button.tabIndex = selected ? 0 : -1;
      button.classList.toggle('on', selected);
      if (!radio) button.setAttribute('aria-selected', String(selected));
    });
    onNavigation?.();
  }

  function render(data, screen = []) {
    cancelTimeout();
    // A manual refresh can finish just after the user selects an option.
    if (!data.revision || data.revision !== current?.revision) cursor = data.selected || 0;
    current = data;
    currentScreen = screen;
    if (cursorMessage) {
      cursorMessage.hidden = data.source !== 'cursor_menu_notice' || !Array.isArray(data.choices);
      if (!cursorMessage.hidden) {
        $().hidden = true;
        cursorMessage.querySelector('h3').textContent = data.hint;
        const items = data.choices.map((text, i) => {
          const row = document.createElement('li');
          row.textContent = text;
          if (i === data.selected) {
            row.setAttribute('aria-current', 'true');
            row.append(document.createTextNode('（当前）'));
          }
          return row;
        });
        cursorMessage.querySelector('ol').replaceChildren(...items);
        onNavigation?.();
        return;
      }
    }
    $('title').textContent = data.title || (data.kind === 'confirm' ? '需要确认' : '等待选择');
    const hint = $('hint');
    hint.textContent = data.hint || '';
    hint.hidden = !hint.textContent;

    const followup = data.source === 'codex_followup';
    const screenOnly = data.source === 'codex_screen_notice';
    if (screenOnly) $('title').textContent = '命令等待终端审批';
    $('terminal').textContent = screenOnly ? '在终端审批' : followup ? '展开终端作答' : '实时作答';
    const list = $('options');
    list.innerHTML = '';
    const approval = codexCard(data) && actionable(data);
    list.setAttribute('role', approval ? 'radiogroup' : 'listbox');
    if (approval) list.removeAttribute('aria-multiselectable');
    else list.setAttribute('aria-multiselectable', String(data.kind === 'multi'));
    data.options.forEach((option, index) => {
      const button = document.createElement(approval ? 'label' : 'button');
      if (approval) {
        const input = document.createElement('input');
        input.type = 'radio';
        input.name = 'codex-command-choice';
        button.appendChild(input);
      } else {
        button.type = 'button';
        button.setAttribute('role', 'option');
      }
      button.className = 'prompt-option';
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
      button.addEventListener(approval ? 'change' : 'click', () => {
        if (data !== current || button.disabled) return;
        cursor = index;
        paintCursor();
      });
      list.appendChild(button);
    });

    const nav = $('nav');
    nav.hidden = !actionable(data) || !data.options.length;
    for (const button of nav.children) {
      button.hidden = ['Tab', 'BTab'].includes(button.dataset.nav) && !(data.question_count > 1);
    }
    $('confirm').hidden = !data.options.length || (!actionable(data) && !codexCard(data));
    $('state').textContent = '';
    $('raw').textContent = screen.join('\n');
    $('raw-wrap').hidden = !screen.length;
    $('raw-wrap').open = data.kind === 'screen';
    $().hidden = false;
    paintCursor();
  }

  function showStatus(found) {
    if (!automatic || !paneNow() || sending || reading || followTimer || document.hidden) return;
    if (hookChecked && screenNotice?.source === 'codex_screen_notice' && codexCard(found)) found = screenNotice;
    if (actionable(screenNotice) && codexCard(screenNotice) && screenNotice.screen_revision === observedScreen) found = screenNotice;
    if (codexCard(found) && observedScreen !== null && found.screen_revision && found.screen_revision !== observedScreen) found = null;
    if (found?.source === 'codex_screen_notice' && observedScreen !== null && found.revision !== 'screen-' + observedScreen) found = null;
    if (found?.source === 'cursor_menu_notice' && observedScreen !== null) found = screenNotice;
    if (found?.source === 'codex_screen_notice' && found.revision === screenNotice?.revision) found = screenNotice;
    found ||= screenNotice;
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
    if (current?.revision === lastStatus) {
      if (found.source === 'cursor_menu_notice' && found.selected !== current.selected) render(found);
      return;
    }
    if (suppressed === lastStatus && Date.now() < reshowAt) return;
    manual = false;
    render(found);
    if (['cursor_menu_notice', 'codex_screen_notice'].includes(found.source)) return;
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
      if (actionable(current)) focusedOption()?.focus();
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
        focusedOption()?.focus({preventScroll: true});
        $('options').children[cursor]?.scrollIntoView?.({block: 'nearest'});
      }
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
    $('confirm').onclick = () => submit({index: cursor});
    $('close').onclick = () => {
      suppressed = current?.revision || lastStatus;
      reshowAt = Infinity;
      refresh(false);
    };
    $('terminal').onclick = () => {
      if (['codex_screen_notice', 'codex_followup'].includes(current?.source)) openInlineTerminal();
      else document.getElementById('chat-reply-panel')?.dispatchEvent(new Event('open-reply'));
    };
    $('nav').addEventListener('click', event => {
      const button = event.target.closest('button[data-nav]');
      if (button && actionable(current)) navigate(button.dataset.nav);
    });
    $().addEventListener('keydown', event => {
      if ((!actionable(current) && !codexCard(current)) || event.target?.closest?.('.prompt-foot,.prompt-head,.prompt-nav,summary')) return;
      let key = event.key;
      if (current.question_count > 1 && ['ArrowLeft', 'ArrowRight'].includes(key)) {
        key = key === 'ArrowLeft' || event.shiftKey ? 'BTab' : 'Tab';
      } else if (!['ArrowDown', 'ArrowUp', 'Escape'].includes(key)) return;
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
      screenNotice = null;
      observedScreen = null;
      hookChecked = false;
      suppressed = '';
      lastStatus = '';
      showStatus(sharedStatus());
    }
  }

  function start(getter, status, onNotice, onNavigationChange, openTerminal) {
    openInlineTerminal = openTerminal;
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
