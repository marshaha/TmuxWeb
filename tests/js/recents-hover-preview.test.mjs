// Hover preview for the sidebar "最近" list (web/assets/pane-preview.js).
// Run with: node --test tests/js/recents-hover-preview.test.mjs
//
// The reported bug: on a recents row, the preview only seemed to appear when
// the mouse was parked on the number. Root cause: every pointerover on a
// descendant of the row cleared and restarted the delay timer, and a recents
// row is one line of many small children (number / dot / title / time /
// actions) — sliding the mouse from the number to the title crosses several
// boundaries, so the 2s delay kept restarting and the preview only appeared
// if the mouse sat perfectly still on one single child for the full delay.
// The server list did not feel this way: its title/meta blocks span the
// whole width, so small drifts stay inside one element.
//
// Fix: bind() takes an opt-in steadyDelay flag. With it on, the delay counts
// from first entering the row; moving between children of the same pane only
// updates the anchor (for placement) and does not restart the timer. Moving
// to a different pane, or leaving the row, still restarts/cancels as before.
// The server list bind does not pass the flag, so its timing is unchanged.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.dirname(new URL(import.meta.url).pathname);
const asset = fs.readFileSync(
  path.join(root, '..', '..', 'web', 'assets', 'pane-preview.js'), 'utf8');

const DELAY = 200;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function matches(el, sel) {
  if (!el) return false;
  if (sel.startsWith('.')) return el.classList && el.classList.contains(sel.slice(1));
  if (sel.startsWith('#')) return el.id === sel.slice(1);
  if (sel.startsWith('[') && sel.endsWith(']')) {
    const key = sel.slice(1, -1).replace(/^data-/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return el.dataset && key in el.dataset;
  }
  return false;
}

function makeEl(tag, cls = '', dataset = {}) {
  const classes = new Set(cls.split(' ').filter(Boolean));
  const el = {
    tagName: tag.toUpperCase(),
    id: '',
    children: [],
    parentNode: null,
    dataset: { ...dataset },
    style: {},
    hidden: false,
    textContent: '',
    _html: '',
    _listeners: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, f) => { if (f === undefined) f = !classes.has(c); f ? classes.add(c) : classes.delete(c); },
    },
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
    querySelector(sel) {
      const walk = (n) => {
        for (const c of n.children) { if (matches(c, sel)) return c; const r = walk(c); if (r) return r; }
        return null;
      };
      return walk(this);
    },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, right: 300, top: 100, bottom: 128, width: 300, height: 28 }; },
    contains(node) { let n = node; while (n) { if (n === this) return true; n = n.parentNode; } return false; },
    closest(sel) { let n = this; while (n) { if (matches(n, sel)) return n; n = n.parentNode; } return null; },
    setAttribute() {},
    getAttribute() { return null; },
    get scrollHeight() { return 260; },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._html; },
    set(html) {
      el._html = html;
      el.children = [];
      for (const m of html.matchAll(/id="([^"]+)"/g)) {
        const child = makeEl('div');
        child.id = m[1];
        el.appendChild(child);
      }
    },
  });
  return el;
}

function dispatch(target, type, extra = {}) {
  const event = { type, target, pointerType: 'mouse', ...extra, preventDefault() {}, stopPropagation() {} };
  let node = target;
  while (node) {
    for (const fn of (node._listeners[type] || [])) fn(event);
    node = node.parentNode;
  }
}

// One recents row, same structure as recents.js rowHtml:
// .recent-row > .recent-open(.pane-num, .dot, .label, .when) + .recent-act buttons.
function recentRow(paneId, number, labelText) {
  const row = makeEl('div', 'recent-row', { row: paneId });
  const open = makeEl('button', 'recent-open', { recent: paneId });
  row.appendChild(open);
  const num = makeEl('span', 'pane-num'); num.textContent = `#${number}`;
  const dot = makeEl('span', 'dot idle');
  const label = makeEl('span', 'label'); label.textContent = labelText;
  const when = makeEl('span', 'when'); when.textContent = '5 分钟';
  open.appendChild(num); open.appendChild(dot); open.appendChild(label); open.appendChild(when);
  const pin = makeEl('button', 'recent-act', { pin: paneId });
  const star = makeEl('button', 'recent-act', { star: paneId });
  row.appendChild(pin); row.appendChild(star);
  return { row, open, num, dot, label, when, pin, star };
}

// One server-list pane button, same structure as console-app.js renderList.
function serverPane(paneId) {
  const btn = makeEl('button', 'pane', { pane: paneId });
  const paneRow = makeEl('div', 'pane-row');
  const dot = makeEl('span', 'dot');
  const num = makeEl('span', 'pane-num'); num.textContent = '#39';
  const target = makeEl('span', 'pane-target'); target.textContent = 'pane 0 · hermes';
  paneRow.appendChild(dot); paneRow.appendChild(num); paneRow.appendChild(target);
  const title = makeEl('div', 'pane-title'); title.textContent = 'hermes 会话标题';
  const meta = makeEl('div', 'pane-meta'); meta.textContent = '/Users/mars/hermes';
  btn.appendChild(paneRow); btn.appendChild(title); btn.appendChild(meta);
  return { btn, paneRow, dot, num, target, title, meta };
}

function load() {
  const body = makeEl('body');
  const sandbox = {
    window: null,
    document: {
      body,
      createElement: (tag) => makeEl(tag),
      addEventListener: () => {},
    },
    matchMedia: (q) => ({ matches: q.includes('hover: hover') ? true : false }),
    setTimeout, clearTimeout,
    console,
  };
  sandbox.window = sandbox;
  sandbox.window.matchMedia = sandbox.matchMedia;
  sandbox.window.innerWidth = 1200;
  sandbox.window.innerHeight = 800;
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);
  vm.runInContext(asset, sandbox, { filename: 'pane-preview.js' });
  const P = sandbox.TmuxPanePreview;

  const recentList = makeEl('div', 'recent-list');
  recentList.id = 'recent-list';
  body.appendChild(recentList);
  const paneList = makeEl('div');
  paneList.id = 'pane-list';
  body.appendChild(paneList);

  const a = recentRow('pane-1', 39, 'hermes 会话标题');
  const b = recentRow('pane-2', 42, 'kimi 会话标题');
  recentList.appendChild(a.row);
  recentList.appendChild(b.row);
  const server = serverPane('pane-1');
  paneList.appendChild(server.btn);

  const entries = new Map([
    ['pane-1', {
      pane: 'pane-1', provider: 'hermes', status: 'idle',
      agent_preview: { role: 'assistant', text: '最后一条 agent 回复', timestamp: Date.now() / 1000 - 60 },
    }],
    ['pane-2', {
      pane: 'pane-2', provider: 'kimi', status: 'idle',
      agent_preview: { role: 'assistant', text: '另一条回复', timestamp: Date.now() / 1000 - 120 },
    }],
  ]);
  P.mount({ data: () => entries, delay: () => DELAY, render: null, onChat: null, onFull: null, fetch: null });
  P.bind(paneList, { selector: '[data-pane]', paneOf: (el) => el.dataset.pane });
  P.bind(recentList, { selector: '.recent-row', paneOf: (el) => el.dataset.row, steadyDelay: true });
  return { P, body, a, b, server };
}

async function leave(P, body, from) {
  dispatch(from, 'pointerout', { relatedTarget: body });
  P.hide();
  await sleep(30);
}

test('parking on the number shows the preview after the delay', async () => {
  const { P, body, a } = load();
  dispatch(a.num, 'pointerover');
  assert.equal(P.state().visible, false);
  await sleep(DELAY + 100);
  assert.equal(P.state().visible, true);
  assert.equal(P.state().pane, 'pane-1');
  await leave(P, body, a.num);
});

test('parking on the title shows the preview after the same delay', async () => {
  const { P, body, a } = load();
  dispatch(a.label, 'pointerover');
  await sleep(DELAY + 100);
  assert.equal(P.state().visible, true);
  assert.equal(P.state().pane, 'pane-1');
  await leave(P, body, a.label);
});

test('parking on an action button shows the preview', async () => {
  const { P, body, a } = load();
  dispatch(a.pin, 'pointerover');
  await sleep(DELAY + 100);
  assert.equal(P.state().visible, true);
  assert.equal(P.state().pane, 'pane-1');
  await leave(P, body, a.pin);
});

test('moving within the row does not restart the delay', async () => {
  const { P, body, a } = load();
  dispatch(a.num, 'pointerover');               // enter at t=0
  await sleep(150);
  dispatch(a.num, 'pointerout', { relatedTarget: a.label });
  dispatch(a.label, 'pointerover');             // same row, different child
  // fixed: fires DELAY after entry (t=200); without the fix it would fire
  // DELAY after the hop (t=350), so at t=275 it must already be visible.
  await sleep(125);
  assert.equal(P.state().visible, true);
  assert.equal(P.state().pane, 'pane-1');
  await leave(P, body, a.label);
});

test('without steadyDelay the same move restarts the delay (server list unchanged)', async () => {
  const { P, body, server } = load();
  dispatch(server.num, 'pointerover');          // enter at t=0
  await sleep(150);
  dispatch(server.num, 'pointerout', { relatedTarget: server.title });
  dispatch(server.title, 'pointerover');        // same pane, different child
  await sleep(125);                             // t=275: old timer would fire at t=350
  assert.equal(P.state().visible, false);
  await sleep(175);                             // t=450: fires after the hop
  assert.equal(P.state().visible, true);
  assert.equal(P.state().pane, 'pane-1');
  await leave(P, body, server.title);
});

test('moving to a different row restarts the delay for the new row', async () => {
  const { P, body, a, b } = load();
  dispatch(a.num, 'pointerover');               // row A at t=0
  await sleep(150);
  dispatch(a.num, 'pointerout', { relatedTarget: b.label });
  dispatch(b.label, 'pointerover');             // row B at t=150
  await sleep(125);                             // t=275: neither timer has fired
  assert.equal(P.state().visible, false);
  await sleep(175);                             // t=450: B fired at t=350
  assert.equal(P.state().visible, true);
  assert.equal(P.state().pane, 'pane-2');
  await leave(P, body, b.label);
});

test('leaving the row before the delay cancels the preview', async () => {
  const { P, body, a } = load();
  dispatch(a.num, 'pointerover');
  await sleep(100);
  dispatch(a.num, 'pointerout', { relatedTarget: body });
  await sleep(DELAY + 150);
  assert.equal(P.state().visible, false);
  assert.equal(P.state().pane, null);
  await leave(P, body, a.num);
});

test('parking on a server-list pane uses the same delay', async () => {
  const { P, body, server } = load();
  dispatch(server.title, 'pointerover');
  await sleep(DELAY + 100);
  assert.equal(P.state().visible, true);
  assert.equal(P.state().pane, 'pane-1');
  await leave(P, body, server.title);
});
