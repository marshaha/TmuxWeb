// Unit tests for the unified interaction card (web/assets/prompt-card.js).
// Run with: node --test tests/js/prompt-card.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.dirname(new URL(import.meta.url).pathname);
const source = fs.readFileSync(
  path.join(root, '..', '..', 'web', 'assets', 'prompt-card.js'), 'utf8');

const CARD_IDS = ['prompt-card', 'prompt-card-title', 'prompt-card-hint',
  'prompt-card-nav', 'prompt-card-options', 'prompt-card-toggles', 'prompt-card-state',
  'prompt-card-confirm', 'prompt-card-close', 'prompt-card-terminal', 'prompt-card-timeout',
  'prompt-card-raw', 'prompt-card-raw-wrap'];

function makeElement(tag = 'div') {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    children: [],
    dataset: {},
    hidden: false,
    disabled: false,
    textContent: '',
    className: '',
    onclick: null,
    _listeners: {},
    _classSet: new Set(),
    setAttribute(key, value) { el[key] = value; },
    addEventListener(type, fn) { (el._listeners[type] ||= []).push(fn); },
    appendChild(child) { el.children.push(child); return child; },
    querySelectorAll() { return []; },
    focus() {},
  };
  el.classList = {
    add: cls => el._classSet.add(cls),
    remove: cls => el._classSet.delete(cls),
    toggle: (cls, force) => {
      if (force === undefined) el._classSet.has(cls) ? el._classSet.delete(cls) : el._classSet.add(cls);
      else if (force) el._classSet.add(cls);
      else el._classSet.delete(cls);
    },
    contains: cls => el._classSet.has(cls),
  };
  Object.defineProperty(el, 'innerHTML', {
    get: () => el._html || '',
    set: value => { el._html = value; el.children = []; },
  });
  return el;
}

function makeDocument() {
  const byId = {};
  for (const id of CARD_IDS) byId[id] = makeElement('div');
  return {
    hidden: false,
    getElementById: id => byId[id] || null,
    createElement: tag => makeElement(tag),
    querySelectorAll: () => [],
    addEventListener() {},
    _byId: byId,
  };
}

function makeClock() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  return {
    now: () => now,
    setTimeout: (fn, ms) => { const id = nextId++; timers.set(id, {at: now + ms, ms, fn, repeat: false}); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, ms) => { const id = nextId++; timers.set(id, {at: now + ms, ms, fn, repeat: true}); return id; },
    clearInterval: id => timers.delete(id),
    advance(ms) {
      const end = now + ms;
      for (;;) {
        let due = null;
        for (const [id, t] of timers) {
          if (t.at <= end && (!due || t.at < due.t.at)) due = {id, t};
        }
        if (!due) break;
        now = due.t.at;
        timers.delete(due.id);
        if (due.t.repeat) timers.set(due.id, {at: now + due.t.ms, ms: due.t.ms, fn: due.t.fn, repeat: true});
        due.t.fn();
      }
      now = end;
    },
  };
}

const MENU = {kind: 'menu', title: 'Select a model', hint: '↑↓ navigate · Enter select',
  selected: 0, verified: true, read_only: false, revision: 'r1', source: 'hermes_clarify',
  options: [
    {label: 'Alpha', number: '1'},
    {label: 'Beta', number: '2'},
  ],
  toggles: []};

function loadCard({prompt = MENU, chooseHandler, inspectHandler} = {}) {
  const clock = makeClock();
  const doc = makeDocument();
  let current = prompt;
  const calls = [];
  const fetch = async (url, options) => {
    calls.push({url, options});
    if (url.includes('/prompt/choose')) {
      if (chooseHandler) return chooseHandler(url, options);
      return {ok: true, status: 200, json: async () => ({})};
    }
    if (url.includes('/prompt?inspect=true')) {
      if (inspectHandler) return inspectHandler();
      return {ok: true, status: 200, json: async () => ({prompt: current})};
    }
    return {ok: true, status: 200, json: async () => ({})};
  };
  const sandbox = {
    window: {}, document: doc, location: {}, fetch, AbortController,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    Date: {now: clock.now},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, {filename: 'prompt-card.js'});
  const api = sandbox.TmuxPrompt;
  const card = () => doc._byId['prompt-card'];
  const optionsEl = () => doc._byId['prompt-card-options'];
  return {api, clock, doc, calls, card, optionsEl, setPrompt: next => { current = next; }};
}

// poll 是 async：让 fetch 的 promise 跑完
async function flush() {
  for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve));
}

function click(el) {
  for (const fn of el._listeners.click || []) fn();
}

function chooseCalls(calls) {
  return calls.filter(call => call.url.includes('/prompt/choose'));
}

test('a verified choice is sent once with its revision, including while the first request is pending', async () => {
  let finish;
  const {api, card, calls, optionsEl, doc} = loadCard({chooseHandler: () => new Promise(r => { finish = r; })});
  api.start(() => '%1', () => MENU);
  const option = optionsEl().children[1];
  click(option);
  click(optionsEl().children[0]);
  click(option);
  assert.equal(card().hidden, false);
  assert.equal(chooseCalls(calls).length, 0);
  doc._byId['prompt-card-confirm'].onclick();
  doc._byId['prompt-card-confirm'].onclick();
  assert.equal(card().hidden, true);
  assert.deepEqual(chooseCalls(calls).map(c => JSON.parse(c.options.body)), [{index: 1, revision: 'r1'}]);
  finish({ok: true, json: async () => ({ok: true})});
  await flush();
  api.showStatus(MENU);
  assert.equal(card().hidden, true);
});

test('a failed submission explains the failure and requires a fresh inspection', async () => {
  const {api, calls, optionsEl, doc} = loadCard({chooseHandler: async () => ({
    ok: false, status: 409, json: async () => ({detail: 'request resolved'}),
  })});
  api.start(() => '%1', () => MENU);
  click(optionsEl().children[0]);
  assert.equal(calls.length, 0);
  doc._byId['prompt-card-confirm'].onclick();
  await flush();
  assert.equal(optionsEl().children.length, 0);
  assert.equal(doc._byId['prompt-card-hint'].textContent, 'request resolved');
  assert.equal(chooseCalls(calls).length, 1);
});

test('legacy snapshots and read-only notices cannot submit, even with attached key sequences', () => {
  for (const data of [{...MENU, verified: false}, {...MENU, source: 'screen'},
    {...MENU, read_only: true}, {...MENU, revision: ''}]) {
    const {api, calls, optionsEl} = loadCard();
    api.start(() => '%1', () => data);
    for (const option of optionsEl().children) {
      assert.equal(option.disabled, true);
      click(option);
    }
    assert.equal(calls.length, 0);
  }
});

test('timeout, cooldown, and manual close never answer or schedule another screen poll', () => {
  const {api, clock, card, calls, doc} = loadCard();
  api.configure({timeoutMs: 2000, reshowCooldownMs: 5000});
  api.start(() => '%1', () => MENU);
  assert.equal(card().hidden, false);
  clock.advance(2000);
  assert.equal(card().hidden, true);
  api.showStatus(MENU);
  assert.equal(card().hidden, true);
  clock.advance(5000);
  api.showStatus(MENU);
  assert.equal(card().hidden, false);
  doc._byId['prompt-card-close'].onclick();
  clock.advance(60000);
  api.showStatus(MENU);
  assert.equal(card().hidden, true);
  assert.equal(calls.length, 0);
  api.showStatus(null);
  api.showStatus(MENU);
  assert.equal(card().hidden, false);
});

test('a pending read of the same request preserves the staged choice', async () => {
  let finish;
  const {api, optionsEl, calls} = loadCard({inspectHandler: () => new Promise(r => { finish = r; })});
  api.start(() => '%1', () => MENU);
  const read = api.inspect();
  click(optionsEl().children[1]);
  finish({ok: true, json: async () => ({prompt: MENU})});
  await read;
  assert.equal(optionsEl().children[1]['aria-selected'], 'true');
  assert.equal(chooseCalls(calls).length, 0);
});

test('an inspection arriving after a pane change cannot replace or answer the new card', async () => {
  let release, selected = '%1';
  const {api, card, calls} = loadCard({inspectHandler: () => new Promise(r => { release = r; })});
  api.start(() => selected, () => null);
  const inspection = api.inspect();
  selected = '%2';
  api.refresh();
  release({ok: true, json: async () => ({prompt: MENU, screen: []})});
  await inspection;
  assert.equal(card().hidden, true);
  assert.equal(chooseCalls(calls).length, 0);
});

test('switching questions keeps one inline card visible and waits for a fresh revision', async () => {
  const first = {...MENU, question_count: 2};
  const {api, card, clock, doc, optionsEl, calls, setPrompt} = loadCard({prompt: first});
  api.start(() => '%1', () => first);
  const pressNext = () => card()._listeners.keydown[0]({key: 'ArrowRight', preventDefault() {}});
  pressNext();
  pressNext();
  assert.equal(card().hidden, false);
  assert.equal(optionsEl().children.length, 0);
  assert.equal(chooseCalls(calls).length, 1);
  await flush();
  setPrompt({...first, title: 'Question 2', revision: 'r2'});
  clock.advance(200);
  await flush();
  assert.equal(doc._byId['prompt-card-title'].textContent, 'Question 2');
  assert.equal(optionsEl().children.length, 2);
});

test('a screen-only Codex option rechecks the live frame before opening the terminal', async () => {
  const preview = {...MENU, source: 'codex_command_approval', read_only: true, verified: false, screen_revision: 'frame'};
  const {api, calls, optionsEl, doc} = loadCard({inspectHandler: () => ({
    ok: true, json: async () => ({prompt: null}),
  })});
  api.start(() => '%1', () => preview);
  assert.equal(optionsEl().children[0].disabled, false);
  click(optionsEl().children[0]);
  assert.equal(calls.length, 0);
  doc._byId['prompt-card-confirm'].onclick();
  await flush();
  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.endsWith('/prompt?inspect=true&fresh=true'));
  assert.equal(chooseCalls(calls).length, 0);
  assert.match(doc._byId['prompt-card-hint'].textContent, /审批已结束或已更换/);
});

test('an idle tail retires a manually inspected approval even if the older status is delivered again', async () => {
  const preview = {...MENU, source: 'codex_command_approval', read_only: true, verified: false, screen_revision: 'frame'};
  const {api, card} = loadCard({prompt: preview});
  api.start(() => '%1', () => null);
  await api.inspect();
  api.observeCodexScreen('%2', '');
  assert.equal(card().hidden, false);
  api.observeCodexScreen('%1', '');
  assert.equal(card().hidden, true);
  api.showStatus(preview);
  assert.equal(card().hidden, true);
  api.observeCodexScreen('%1', 'frame');
  api.showStatus(preview);
  assert.equal(card().hidden, false);
});
