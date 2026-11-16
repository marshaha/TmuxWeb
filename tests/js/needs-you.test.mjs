// Unit tests for the "needs you" console card (web/assets/needs-you.js).
// Run with: node --test tests/js/needs-you.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.dirname(new URL(import.meta.url).pathname);
const source = fs.readFileSync(
  path.join(root, '..', '..', 'web', 'assets', 'needs-you.js'), 'utf8');

function load() {
  const sandbox = {window: {}, document: {hidden: false, addEventListener() {}}, location: {}};
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, {filename: 'needs-you.js'});
  return sandbox.TmuxNeedsYou;
}

const needsYou = load();

test('needs-input panes sort first, the rest keep their order', () => {
  const ids = (panes, wanted) => Array.from(needsYou.sortNeedsFirst(panes, wanted), pane => pane.id);
  const panes = [{id: '%4'}, {id: '%5'}, {id: '%6'}];
  assert.deepEqual(ids(panes, new Set(['%6'])), ['%6', '%4', '%5']);
  assert.deepEqual(ids(panes, ['%6', '%5']), ['%5', '%6', '%4'],
    'needs panes come first, in their original order — the set is not an ordering');
  assert.deepEqual(ids(panes, new Set()), ['%4', '%5', '%6']);
  // the input is not mutated: the tree/list render from the same array again
  assert.deepEqual(Array.from(panes, pane => pane.id), ['%4', '%5', '%6']);
});

test('the prompt signature changes when the menu changes', () => {
  const menu = {kind: 'menu', title: 'Do you want to proceed?', selected: 0,
    options: [{label: 'Yes'}, {label: 'No'}], toggles: []};
  const moved = {...menu, selected: 1};
  const relabelled = {...menu, options: [{label: 'Yes'}, {label: 'Nope'}]};
  assert.equal(needsYou.signatureOf(menu), needsYou.signatureOf({...menu}));
  assert.notEqual(needsYou.signatureOf(menu), needsYou.signatureOf(moved));
  assert.notEqual(needsYou.signatureOf(menu), needsYou.signatureOf(relabelled));
  assert.equal(needsYou.signatureOf(null), '');
});

test('the card answers only through /api/panes/{id}/key', () => {
  assert.match(source, /\/api\/panes\/\$\{encodeURIComponent\(pane\)\}\/key/);
  assert.doesNotMatch(source, /prompt\/choose/);
  assert.doesNotMatch(source, /\/api\/panes\/.*\/input/);
});

// ---- 超时 + 描述行的 DOM 测试：fake document + 可控时钟 ----

const CARD_IDS = ['needs-you-card', 'needs-you-card-title', 'needs-you-card-hint',
  'needs-you-card-options', 'needs-you-card-toggles', 'needs-you-card-state',
  'needs-you-card-timeout', 'needs-you-card-close', 'needs-you-card-esc'];

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

const MENU = {kind: 'menu', title: 'Which approach should I use?', hint: 'Enter select · Esc cancel',
  selected: 0,
  options: [
    {label: 'Quick fix', description: 'Patch the bug in place', recommended: true, keys: ['Enter']},
    {label: 'Full refactor', description: 'Restructure the module', keys: ['Down', 'Enter']},
  ],
  toggles: []};

function loadCard({prompt = MENU, timeoutMs, reshowCooldownMs} = {}) {
  const clock = makeClock();
  const doc = makeDocument();
  let current = prompt;
  const calls = [];
  const fetch = async (url, options) => {
    calls.push({url, options});
    if (url === '/api/panes/status') {
      return {ok: true, status: 200,
        json: async () => ({panes: current ? [{id: '%1', needs_input: true, prompt: current}] : [{id: '%1'}]})};
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
  vm.runInContext(source, sandbox, {filename: 'needs-you.js'});
  const api = sandbox.TmuxNeedsYou;
  if (timeoutMs !== undefined || reshowCooldownMs !== undefined) api.configure({timeoutMs, reshowCooldownMs});
  const card = () => doc._byId['needs-you-card'];
  const optionsEl = () => doc._byId['needs-you-card-options'];
  return {api, clock, doc, calls, card, optionsEl, setPrompt: next => { current = next; }};
}

// poll 是 async：让 fetch 的 promise 跑完
async function flush() {
  for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve));
}

function optionParts(button) {
  const text = button.children[0];
  assert.equal(text.className, 'prompt-option-text');
  const label = text.children[0];
  const desc = text.children[1] || null;
  return {text, label, desc};
}

test('the card shows option descriptions and marks only the first option Recommended', async () => {
  const {api, card, optionsEl, doc} = loadCard();
  api.start(() => '%1');
  await flush();
  assert.equal(card().hidden, false);
  const buttons = optionsEl().children;
  assert.equal(buttons.length, 2);
  const first = optionParts(buttons[0]);
  assert.equal(first.label.textContent, 'Quick fix');
  assert.equal(first.label.children.length, 1);
  assert.equal(first.label.children[0].className, 'prompt-rec');
  assert.equal(first.label.children[0].textContent, '推荐');
  assert.equal(first.desc.className, 'prompt-desc');
  assert.equal(first.desc.textContent, 'Patch the bug in place');
  const second = optionParts(buttons[1]);
  assert.equal(second.label.children.length, 0, '只有首选项打推荐标');
  assert.equal(second.desc.textContent, 'Restructure the module');
  assert.equal(doc._byId['needs-you-card-timeout'].textContent, '5:00 后自动收起');
});

test('the card hides itself after the timeout without sending any key', async () => {
  const {api, clock, card, calls} = loadCard({timeoutMs: 2000});
  api.start(() => '%1');
  await flush();
  assert.equal(card().hidden, false);
  clock.advance(1999);
  await flush();
  assert.equal(card().hidden, false, '超时前一刻还亮着');
  clock.advance(1);
  await flush();
  assert.equal(card().hidden, true);
  assert.equal(calls.filter(call => call.url.includes('/key')).length, 0,
    '超时收起不作答、不发键');
});

test('after the timeout the card stays hidden through the cooldown, then comes back on the next sweep', async () => {
  const {api, clock, card, calls} = loadCard({timeoutMs: 2000, reshowCooldownMs: 60000});
  api.start(() => '%1');
  await flush();
  assert.equal(card().hidden, false);
  clock.advance(2000);   // 超时收起
  await flush();
  assert.equal(card().hidden, true);
  clock.advance(8000);   // t=10000：下一轮 poll，冷却期内不重弹
  await flush();
  assert.equal(card().hidden, true, '冷却期内门控仍成立也不重弹');
  clock.advance(60000);  // t=70000：冷却过了，下一轮 poll 重新出现
  await flush();
  assert.equal(card().hidden, false, '冷却过后、等待还在，卡片重新出现');
  assert.equal(calls.filter(call => call.url.includes('/key')).length, 0);
});

test('configure changes the timeout and rejects nonsense values', () => {
  const {api} = loadCard();
  // settings() 返回的是 vm realm 里的对象：按字段断言，别用 deepEqual
  const seen = () => JSON.stringify(api.settings());
  assert.equal(seen(), JSON.stringify({timeoutMs: 300000, reshowCooldownMs: 60000}));
  assert.equal(JSON.stringify(api.configure({timeoutMs: 45000})),
    JSON.stringify({timeoutMs: 45000, reshowCooldownMs: 60000}));
  assert.equal(JSON.stringify(api.configure({timeoutMs: -5, reshowCooldownMs: 'soon'})),
    JSON.stringify({timeoutMs: 45000, reshowCooldownMs: 60000}));
  assert.equal(seen(), JSON.stringify({timeoutMs: 45000, reshowCooldownMs: 60000}));
});

test('closing the card by hand never brings it back for the same menu', async () => {
  const {api, clock, card, doc, calls} = loadCard({timeoutMs: 2000, reshowCooldownMs: 1000});
  api.start(() => '%1');
  await flush();
  assert.equal(card().hidden, false);
  doc._byId['needs-you-card-close'].onclick();
  assert.equal(card().hidden, true);
  clock.advance(100000);   // 很多轮 poll 过去：手动收起不是超时收起
  await flush();
  assert.equal(card().hidden, true, '手动收起的同一道题不再弹');
  assert.equal(calls.filter(call => call.url.includes('/key')).length, 0);
});

test('when the wait is gone the card stays hidden and the cooldown is cleared', async () => {
  const {api, clock, card, setPrompt} = loadCard({timeoutMs: 2000, reshowCooldownMs: 60000});
  api.start(() => '%1');
  await flush();
  assert.equal(card().hidden, false);
  clock.advance(2000);   // 超时收起
  await flush();
  assert.equal(card().hidden, true);
  setPrompt(null);        // 等待结束了（agent 不再需要回答）
  clock.advance(10000);   // 下一轮 poll：门控已不成立
  await flush();
  assert.equal(card().hidden, true);
  setPrompt(MENU);        // 同一道题又出现：冷却已清，不用等冷却
  clock.advance(10000);
  await flush();
  assert.equal(card().hidden, false);
});
