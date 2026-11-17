// Unit tests for the experimental prompt card (web/assets/prompt-card.js).
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
  'prompt-card-options', 'prompt-card-toggles', 'prompt-card-state',
  'prompt-card-close', 'prompt-card-esc', 'prompt-card-enter',
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
  selected: 0,
  options: [
    {label: 'Alpha', number: '1'},
    {label: 'Beta', number: '2'},
  ],
  toggles: []};

function loadCard({prompt = MENU, chooseHandler} = {}) {
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
    if (url.endsWith('/prompt')) {
      return {ok: true, status: 200, json: async () => ({prompt: current})};
    }
    return {ok: true, status: 200, json: async () => ({})};
  };
  const sandbox = {
    window: {}, document: doc, location: {}, fetch,
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

function promptCalls(calls) {
  return calls.filter(call => call.url.endsWith('/prompt'));
}

test('the card answers only through /api/panes/{id}/prompt/choose', () => {
  assert.match(source, /\/api\/panes\/\$\{encodeURIComponent\(pane\)\}\/prompt\/choose/);
  assert.doesNotMatch(source, /\/api\/panes\/.*\/key/);
});

test('clicking an option hides the card at once, before the choose round-trip finishes', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const {api, clock, card, optionsEl, calls} = loadCard({chooseHandler: () => pending});
  api.start(() => '%1');
  await flush();
  assert.equal(card().hidden, false);
  click(optionsEl().children[0]);
  // 同步断言：还没等任何网络回来，卡片已经收了
  assert.equal(card().hidden, true);
  assert.equal(chooseCalls(calls).length, 1);
  release({ok: true, status: 200, json: async () => ({})});
  await flush();
  // 成功后 1.5 秒跟进 poll 看一眼；同一道题还在也不重弹
  const promptBefore = promptCalls(calls).length;
  clock.advance(1500);
  await flush();
  assert.equal(promptCalls(calls).length, promptBefore + 1, '成功后 1.5 秒跟进 poll 还在');
  assert.equal(card().hidden, true, '同一道题还没被 sweep 换掉，不重弹');
});

test('a failed choose brings the card back with the error, and the question can show again', async () => {
  const {api, card, optionsEl, doc, calls} = loadCard({
    chooseHandler: async () => ({ok: false, status: 409, json: async () => ({detail: 'agent busy'})}),
  });
  api.start(() => '%1');
  await flush();
  assert.equal(card().hidden, false);
  click(optionsEl().children[0]);
  assert.equal(card().hidden, true, '点下立刻收卡，不等请求结果');
  await flush();
  // 恢复：卡片重新出现，显示失败原因，选项是点之前那份 prompt
  assert.equal(card().hidden, false);
  assert.equal(doc._byId['prompt-card-state'].textContent, 'agent busy');
  assert.equal(doc._byId['prompt-card-state'].classList.contains('bad'), true);
  assert.equal(optionsEl().children.length, 2);
  assert.equal(chooseCalls(calls).length, 1, '失败只发了一次');
  // 签名已清：refresh 后 poll 不再压住同一道题，卡片重新出现
  api.refresh();
  await flush();
  assert.equal(card().hidden, false);
});

test('a network failure restores the card so the user can retry', async () => {
  const {api, card, optionsEl, doc} = loadCard({
    chooseHandler: async () => { throw new Error('network down'); },
  });
  api.start(() => '%1');
  await flush();
  click(optionsEl().children[0]);
  assert.equal(card().hidden, true);
  await flush();
  assert.equal(card().hidden, false);
  assert.equal(doc._byId['prompt-card-state'].textContent, '没送到，再试一次：network down');
});

test('a second attempt while the first choose is still in flight is blocked', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const {api, card, optionsEl, doc, calls} = loadCard({chooseHandler: () => pending});
  api.start(() => '%1');
  await flush();
  click(optionsEl().children[0]);   // 第一发：挂起
  assert.equal(chooseCalls(calls).length, 1);
  // 卡片已收，但再点一次（Esc 那条路也一样）不能另起一发
  doc._byId['prompt-card-esc'].onclick();
  click(optionsEl().children[1]);
  assert.equal(chooseCalls(calls).length, 1, '忙时不另起请求');
  assert.equal(doc._byId['prompt-card-state'].textContent, '上一条还在发送，稍等一下…');
  // 第一发走完，忙锁放开，也没有补发第二发
  release({ok: true, status: 200, json: async () => ({})});
  await flush();
  assert.equal(chooseCalls(calls).length, 1);
});
