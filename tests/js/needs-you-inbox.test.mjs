// Unit tests for the needs-you inbox drawer (web/assets/needs-you-inbox.js).
// Run with: node --test tests/js/needs-you-inbox.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.dirname(new URL(import.meta.url).pathname);
const source = fs.readFileSync(
  path.join(root, '..', '..', 'web', 'assets', 'needs-you-inbox.js'), 'utf8');

function load() {
  const sandbox = {
    window: {},
    document: {hidden: false, addEventListener() {}, getElementById() { return null; }},
    location: {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, {filename: 'needs-you-inbox.js'});
  return sandbox.TmuxNeedsYouInbox;
}

const inbox = load();

test('the drawer talks to the inbox endpoints Open WebUI defines', () => {
  assert.match(source, /\/api\/needs-you\/inbox/);
  assert.match(source, /'\?limit=' \+ PAGE/);
  assert.match(source, /\/api\/needs-you\/inbox\/count/);
  assert.match(source, /\/api\/needs-you\/inbox\/open/);
  // open-marks-read is a POST, like Open WebUI's /inbox/open
  assert.match(source, /inbox\/open', \{method: 'POST'\}/);
  // no socket.io, no push: the badge reuses the poll rhythm (30s)
  assert.match(source, /COUNT_MS = 30000/);
  assert.doesNotMatch(source, /socket\.io|PushManager|web-push|vapid/i);
});

test('relative times read like a human wrote them', () => {
  const now = Date.now();
  assert.equal(inbox.formatWhen(now - 1000), '刚刚');
  assert.equal(inbox.formatWhen(now - 5 * 60000), '5 分钟前');
  assert.equal(inbox.formatWhen(now - 3 * 3600000), '3 小时前');
  assert.equal(inbox.formatWhen(now - 2 * 86400000), '2 天前');
  assert.match(inbox.formatWhen(now - 40 * 86400000), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  assert.equal(inbox.formatWhen(0), '');
});

test('the menu summary mirrors the server shape: title — options', () => {
  const prompt = {kind: 'menu', title: 'Do you want to proceed?', options: [
    {label: 'Yes', number: '1'}, {label: 'No', number: '2'}]};
  assert.equal(inbox.menuSummary(prompt), 'Do you want to proceed? — 1. Yes / 2. No');
  assert.equal(inbox.menuSummary({kind: 'menu', options: []}), 'menu');
});

test('a live pane row jumps; a closed pane row says so in place', () => {
  const base = {id: 7, pane_id: 'localhost~%4', title: 'Do you want to proceed?',
    summary: 'Do you want to proceed? — 1. Yes / 2. No', created_at: Date.now() / 1000,
    read_at: null, resolved_at: null};
  const live = inbox.itemHtml({...base, pane_alive: true});
  assert.match(live, /data-pane="localhost~%4"/);
  assert.match(live, /data-alive="1"/);
  assert.doesNotMatch(live, /该 pane 已关闭/);
  const closed = inbox.itemHtml({...base, pane_alive: false});
  assert.match(closed, /data-alive="0"/);
  assert.match(closed, /该 pane 已关闭/);
  assert.match(closed, /inbox-item unread closed/);
});

test('unread rows carry the dot, read rows do not', () => {
  const base = {id: 7, pane_id: 'localhost~%4', title: 't', summary: 's',
    created_at: Date.now() / 1000, pane_alive: true};
  assert.match(inbox.itemHtml({...base, read_at: null}), /inbox-item unread/);
  assert.doesNotMatch(inbox.itemHtml({...base, read_at: 1700000000}), /unread/);
});

test('resolved rows say so, and nothing is injected unescaped', () => {
  const row = inbox.itemHtml({id: 7, pane_id: 'localhost~%4',
    title: '<script>alert(1)</script>', summary: 'a & b', created_at: 1700000000,
    read_at: null, resolved_at: 1700000001, pane_alive: true});
  assert.doesNotMatch(row, /<script>alert/);
  assert.match(row, /&lt;script&gt;/);
  assert.match(row, /a &amp; b/);
  assert.match(row, /已解决/);
});

// ---- behaviour tests: open()/close() against a fake DOM + fetch ----
// The 2026-11-17 phone screenshot: the drawer opened with only its header,
// blank body, and the X did nothing. Two causes, both pinned here:
// (1) .inbox-drawer sets display:flex with no [hidden] override, so
// close() setting hidden=true never hid it; (2) open() awaited the
// mark-read POST before loading the list, and fetch had no timeout, so a
// hung request left the drawer blank forever.

test('the hidden drawer really hides: [hidden] beats display:flex', () => {
  const css = fs.readFileSync(
    path.join(root, '..', '..', 'web', 'assets', 'needs-you-inbox.css'), 'utf8');
  assert.match(css, /\.inbox-drawer\[hidden\]\{display:none\}/);
  assert.match(css, /\.inbox-backdrop\{[^}]*z-index:55/);
  assert.match(css, /\.inbox-backdrop\[hidden\]\{display:none\}/);
  const html = fs.readFileSync(
    path.join(root, '..', '..', 'web', 'index.html'), 'utf8');
  assert.match(html, /id="inbox-backdrop"/);
  assert.match(html, /id="inbox-state"/);
});

class FakeAbortSignal {
  constructor() { this.aborted = false; this._fns = []; }
  addEventListener(type, fn) { if (type === 'abort') this._fns.push(fn); }
}
class FakeAbortController {
  constructor() { this.signal = new FakeAbortSignal(); }
  abort() {
    if (this.signal.aborted) return;
    this.signal.aborted = true;
    for (const fn of this.signal._fns) fn();
  }
}

function makeNode(id) {
  const listeners = {};
  const node = {
    id,
    hidden: true,
    textContent: '',
    innerHTML: '',
    onclick: null,
    _listeners: listeners,
    _qsaCalls: [],
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    dispatch(type, event) { (listeners[type] || []).forEach(fn => fn(event || {})); },
    click() { if (this.onclick) this.onclick(); },
    insertAdjacentHTML(_pos, html) { this.innerHTML += html; },
    querySelector() { return null; },
    querySelectorAll(sel) { this._qsaCalls.push(sel); return []; },
  };
  return node;
}

function okJson(data) {
  return () => Promise.resolve({status: 200, ok: true, json: async () => data});
}

// A fetch that never resolves unless its AbortSignal fires: the phone
// network stall, reproduced deterministically.
function hangingFetch() {
  return (path, options = {}) => new Promise((_resolve, reject) => {
    const signal = options.signal;
    if (signal && signal.addEventListener) {
      signal.addEventListener('abort', () => {
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      });
    }
  });
}

function makeFetch(routes) {
  return (path, options = {}) => {
    for (const [key, handler] of Object.entries(routes)) {
      if (path.includes(key)) return Promise.resolve(handler(path, options));
    }
    return Promise.reject(new Error('unexpected fetch: ' + path));
  };
}

const flush = async (n = 10) => {
  for (let i = 0; i < n; i++) await new Promise(resolve => setImmediate(resolve));
};

function loadWithDom(fetchImpl) {
  const nodes = {};
  for (const id of ['inbox-drawer', 'inbox-backdrop', 'inbox-list', 'inbox-empty',
    'inbox-state', 'inbox-status', 'inbox-badge', 'inbox-button', 'inbox-close',
    'inbox-more']) nodes[id] = makeNode(id);
  const timers = [];
  const sandbox = {
    window: {},
    document: {
      hidden: false,
      getElementById(id) { return nodes[id] || null; },
      addEventListener(type, fn) {
        (sandbox._docListeners[type] ||= []).push(fn);
      },
    },
    location: {},
    fetch: fetchImpl,
    setTimeout(fn, ms) {
      const t = {fn, ms, cleared: false};
      timers.push(t);
      return timers.length - 1;
    },
    clearTimeout(id) { if (timers[id]) timers[id].cleared = true; },
    setInterval() { return 0; },
    clearInterval() {},
    AbortController: FakeAbortController,
    _nodes: nodes,
    _timers: timers,
    _docListeners: {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, {filename: 'needs-you-inbox.js'});
  return {inbox: sandbox.TmuxNeedsYouInbox, nodes, timers, sandbox};
}

function fireTimers(timers, ms) {
  for (const t of timers) {
    if (!t.cleared && t.ms === ms) { t.cleared = true; t.fn(); }
  }
}

const sampleItem = {
  id: 1, pane_id: 'localhost~%4', title: 'Do you want to proceed?',
  summary: 'Do you want to proceed? — 1. Yes / 2. No',
  created_at: Date.now() / 1000, read_at: null, resolved_at: null, pane_alive: true,
};

function listingFetch() {
  return makeFetch({
    'inbox/open': okJson({count: 0}),
    'inbox?limit': okJson({items: [sampleItem], next_cursor: null}),
    'inbox/count': okJson({count: 0}),
  });
}

test('close() after open() hides the drawer and the backdrop', async () => {
  const {inbox, nodes} = loadWithDom(listingFetch());
  await inbox.open();
  await flush();
  assert.equal(nodes['inbox-drawer'].hidden, false);
  assert.equal(nodes['inbox-backdrop'].hidden, false);
  assert.match(nodes['inbox-list'].innerHTML, /Do you want to proceed/);
  assert.equal(nodes['inbox-state'].hidden, true);
  inbox.close();
  assert.equal(nodes['inbox-drawer'].hidden, true);
  assert.equal(nodes['inbox-backdrop'].hidden, true);
});

test('a hung mark-read never blocks the list', async () => {
  const {inbox, nodes} = loadWithDom((path, options = {}) => {
    if (path.includes('inbox/open')) return hangingFetch()(path, options);
    if (path.includes('inbox?limit')) {
      return Promise.resolve({status: 200, ok: true,
        json: async () => ({items: [sampleItem], next_cursor: null})});
    }
    return Promise.reject(new Error('unexpected fetch: ' + path));
  });
  await inbox.open();
  await flush();
  assert.equal(nodes['inbox-drawer'].hidden, false);
  assert.match(nodes['inbox-list'].innerHTML, /Do you want to proceed/);
  assert.equal(nodes['inbox-state'].hidden, true);
});

test('a hung list fetch times out and the error shows in the body', async () => {
  const {inbox, nodes, timers} = loadWithDom((path, options = {}) => {
    if (path.includes('inbox/open')) return okJson({count: 0})(path, options);
    if (path.includes('inbox?limit')) return hangingFetch()(path, options);
    return Promise.reject(new Error('unexpected fetch: ' + path));
  });
  const opened = inbox.open();
  await flush();
  assert.equal(nodes['inbox-state'].hidden, false);
  assert.equal(nodes['inbox-state'].textContent, '载入中…');
  fireTimers(timers, 15000);
  await opened;
  await flush();
  assert.match(nodes['inbox-state'].textContent, /没载入：请求超时/);
  assert.equal(nodes['inbox-state'].hidden, false);
  assert.match(nodes['inbox-status'].textContent, /没载入：请求超时/);
  assert.equal(nodes['inbox-list'].innerHTML, '');
});

test('backdrop click, the bell toggle and Escape all close the drawer', async () => {
  const {inbox, nodes, sandbox} = loadWithDom(listingFetch());
  inbox.start({onJump: () => {}});
  await flush();
  nodes['inbox-button'].click();
  await flush();
  assert.equal(nodes['inbox-drawer'].hidden, false);
  assert.equal(nodes['inbox-backdrop'].hidden, false);
  nodes['inbox-backdrop'].click();
  assert.equal(nodes['inbox-drawer'].hidden, true);
  assert.equal(nodes['inbox-backdrop'].hidden, true);
  nodes['inbox-button'].click();
  await flush();
  assert.equal(nodes['inbox-drawer'].hidden, false);
  const handlers = sandbox._docListeners.keydown || [];
  assert.ok(handlers.length > 0);
  handlers.forEach(fn => fn({key: 'Escape'}));
  assert.equal(nodes['inbox-drawer'].hidden, true);
  assert.equal(nodes['inbox-backdrop'].hidden, true);
});
