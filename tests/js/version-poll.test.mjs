// Unit tests for the borrowed version polling + new-version toast in
// web/assets/console-app.js (Open WebUI svelte.config.js pollInterval:60000
// + src/routes/+layout.svelte:111: the client polls the version name every
// 60s and reloads when it changes; TmuxWeb keeps the poll but shows a toast
// with a refresh button instead of silently reloading, so a user mid-input
// never loses state).
// Run with: node --test tests/js/version-poll.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const page = fs.readFileSync(path.join(root, '..', '..', 'web', 'assets', 'console-app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, '..', '..', 'web', 'index.html'), 'utf8');

function grab(name) {
  let start = page.indexOf('function ' + name + '(');
  assert.notEqual(start, -1, `missing function ${name}`);
  // Keep a preceding `async ` so an async function stays async when eval'd:
  // slicing from `function` would turn its `await` into a syntax error.
  if (page.slice(Math.max(0, start - 6), start) === 'async ') start -= 6;
  // Skip the parameter list first: it may itself contain braces, which a
  // naive brace count would mistake for the body open brace.
  let paren = 0, body = -1;
  for (let i = page.indexOf('(', start); i < page.length; i += 1) {
    if (page[i] === '(') paren += 1;
    else if (page[i] === ')') {
      paren -= 1;
      if (!paren) { body = page.indexOf('{', i); break; }
    }
  }
  assert.notEqual(body, -1, `no body for ${name}`);
  let depth = 0;
  for (let i = body; i < page.length; i += 1) {
    if (page[i] === '{') depth += 1;
    else if (page[i] === '}') {
      depth -= 1;
      if (!depth) return page.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced ${name}`);
}

const pureSrc = [grab('shouldCheckVersion'), grab('shouldShowVersionToast'),
  grab('serverVersionBaseline')].join('\n');

function makeApi(document) {
  return new Function('document',
    pureSrc + '\nreturn {shouldCheckVersion, shouldShowVersionToast, serverVersionBaseline};')(document);
}

// --- The decision: show the toast only for a genuinely new version. ---

test('same version is not new', () => {
  const api = makeApi({ querySelector: () => null });
  assert.equal(api.shouldShowVersionToast('abc1234', 'abc1234', '', '', false), false);
});

test('a different version is new', () => {
  const api = makeApi({ querySelector: () => null });
  assert.equal(api.shouldShowVersionToast('abc1234', 'def5678', '', '', false), true);
});

test('no baseline or no fetched version never shows a toast', () => {
  const api = makeApi({ querySelector: () => null });
  assert.equal(api.shouldShowVersionToast('', 'def5678', '', '', false), false);
  assert.equal(api.shouldShowVersionToast('abc1234', '', '', '', false), false);
  assert.equal(api.shouldShowVersionToast('', '', '', '', false), false);
});

test('dismissing a version silences it, but not the next one', () => {
  const api = makeApi({ querySelector: () => null });
  assert.equal(api.shouldShowVersionToast('abc1234', 'def5678', 'def5678', 'def5678', false), false);
  assert.equal(api.shouldShowVersionToast('abc1234', 'aaa0000', 'def5678', 'def5678', false), true);
});

test('an already-visible toast is not shown twice', () => {
  const api = makeApi({ querySelector: () => null });
  assert.equal(api.shouldShowVersionToast('abc1234', 'def5678', 'def5678', '', true), false);
});

test('a toast clobbered by a plain toast comes back on the next check', () => {
  // toast() takes over the #toast element and clears the visible flag; the
  // next version check must re-show the prompt instead of losing it.
  const api = makeApi({ querySelector: () => null });
  assert.equal(api.shouldShowVersionToast('abc1234', 'def5678', 'def5678', '', false), true);
});

// --- The rhythm: piggyback on the 20s pane-list round, every 3rd round. ---

test('version is checked every 3rd pane-list round, about every 60s', () => {
  const api = makeApi({ querySelector: () => null });
  assert.equal(api.shouldCheckVersion(0, 3), false, 'page load is fresh by definition');
  assert.equal(api.shouldCheckVersion(1, 3), false);
  assert.equal(api.shouldCheckVersion(2, 3), false);
  assert.equal(api.shouldCheckVersion(3, 3), true);
  assert.equal(api.shouldCheckVersion(6, 3), true);
  assert.equal(api.shouldCheckVersion(4, 3), false);
  assert.ok(page.includes('VERSION_CHECK_EVERY_ROUNDS=3'), '3 rounds x 20s = 60s, upstream cadence');
  assert.ok(page.includes('state.versionPollRound++;if(shouldCheckVersion(state.versionPollRound,VERSION_CHECK_EVERY_ROUNDS))checkServerVersion();'),
    'the check must ride the existing 20s loadPanes interval, not a new timer');
  assert.ok(!page.includes('setInterval(()=>{if(!document.hidden)checkServerVersion'),
    'no separate version timer');
});

// --- The baseline: the meta tag the server fills in at page-serve time. ---

test('baseline comes from the server-version meta tag, placeholder means none', () => {
  const withMeta = makeApi({ querySelector: () => ({ content: 'abc1234' }) });
  assert.equal(withMeta.serverVersionBaseline(), 'abc1234');
  const noMeta = makeApi({ querySelector: () => null });
  assert.equal(noMeta.serverVersionBaseline(), '');
  const unreplaced = makeApi({ querySelector: () => ({ content: '__SERVER_VERSION__' }) });
  assert.equal(unreplaced.serverVersionBaseline(), '', 'an unreplaced placeholder is not a version');
  const padded = makeApi({ querySelector: () => ({ content: '  abc1234  ' }) });
  assert.equal(padded.serverVersionBaseline(), 'abc1234');
});

test('the console page carries the meta tag and the action-toast styles', () => {
  assert.ok(indexHtml.includes('<meta name="server-version" content="__SERVER_VERSION__">'),
    'server fills the baseline in, like the console hashes');
  assert.ok(indexHtml.includes('.toast.has-action'), 'action toast must be clickable');
  assert.ok(indexHtml.includes('.toast button'), 'refresh / later buttons need styling');
});

// --- The fetch: silent on every failure mode, never a forced reload. ---

test('the check fetches /api/version and silently skips failures', () => {
  const check = grab('checkServerVersion');
  assert.ok(check.includes("fetch('/api/version'"), 'lightweight version endpoint');
  assert.ok(check.includes("'Cache-Control':'no-cache'"), 'request must not be served from cache');
  assert.ok(check.includes('if(!response.ok)return;'),
    'old server without the endpoint (404) or expired session (401) skips silently');
  assert.ok(check.includes("if(!state.serverVersion){state.serverVersion=fetched;return;}"),
    'a page without a baseline adopts the current version instead of prompting');
  assert.ok(check.includes('}catch{'), 'network failure or non-JSON skips silently');
  assert.ok(!check.includes("api('/api/version'"),
    'must not use api(): it redirects to /login on 401 and throws on 404');
  assert.ok(!check.includes('location.reload()'), 'the check itself never reloads');
});

test('the toast offers refresh, never reloads on its own', () => {
  const show = grab('showVersionToast');
  assert.ok(show.includes("label.textContent='服务端已更新，刷新后使用新版'"), 'prompt text');
  assert.ok(show.includes("refresh.textContent='立即刷新'"), 'refresh button');
  assert.ok(show.includes("later.textContent='稍后'"), 'dismiss button');
  assert.ok(show.includes('refresh.onclick=()=>{location.reload();};'),
    'reload happens only when the user taps refresh');
  assert.ok(show.includes('state.versionToast.dismissedFor=version'),
    'dismissing records the version so it is not re-shown');
  assert.ok(show.includes('clearTimeout(toast.timer)'),
    'a pending plain-toast hide timer must not swallow the persistent prompt');
  const reloads = (page.match(/location\.reload\(\)/g) || []).length;
  assert.equal(reloads, 1, 'exactly one reload in the console app: the toast button');
  assert.ok(page.includes('svelte.config.js'), 'borrow source must be cited');
  assert.ok(page.includes('+layout.svelte:111'), 'borrow source must be cited');
  assert.ok(page.includes('pollInterval:60000'), 'upstream 60s cadence must be cited');
});

test('a plain toast taking over the element clears the version-toast state', () => {
  const toastFn = grab('toast');
  assert.ok(toastFn.includes('state.versionToast.visible=false'),
    'the version toast is no longer on screen once a plain toast replaces it');
  assert.ok(toastFn.includes("el.classList.remove('has-action')"),
    'the action layout must not leak into the next plain toast');
});

test('the baseline is read once at boot, before the first pane load', () => {
  assert.ok(page.includes('state.serverVersion=serverVersionBaseline();loadPanes();'),
    'baseline is the version the page was served with');
});

// --- Behaviour harness: the real check + toast, driven against a fake DOM. ---
// The functions are extracted verbatim from console-app.js (same grab as
// above), so these tests run the shipped code, not a re-implementation.

class FakeClassList {
  constructor() { this.s = new Set(); }
  add(...c) { c.forEach((x) => this.s.add(x)); }
  remove(...c) { c.forEach((x) => this.s.delete(x)); }
  contains(c) { return this.s.has(c); }
}

class FakeEl {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.hidden = true;
    this._text = ''; this.classList = new FakeClassList();
    this.onclick = null; this.type = '';
  }
  set textContent(v) { this._text = v; this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  append(...els) { this.children.push(...els); }
}

const behaviourSrc = [grab('toast'), grab('shouldShowVersionToast'),
  grab('showVersionToast'), grab('checkServerVersion')].join('\n');

function makeBehaviourHarness({ baseline, fetchImpl }) {
  const toastEl = new FakeEl('div');
  const state = {
    serverVersion: baseline,
    versionToast: { shownFor: '', dismissedFor: '', visible: false },
  };
  const timers = new Map();
  let nextTimer = 1;
  const rafQueue = [];
  const reloads = [];
  const api = new Function('state', '$', 'document', 'fetch', 'location',
    'requestAnimationFrame', 'clearTimeout', 'setTimeout',
    behaviourSrc + '\nreturn {toast, checkServerVersion, showVersionToast};')(
    state,
    (id) => (id === 'toast' ? toastEl : null),
    {
      querySelector: () => null,
      createElement: (tag) => new FakeEl(tag),
    },
    fetchImpl,
    { reload: () => { reloads.push(1); } },
    (cb) => { rafQueue.push(cb); return rafQueue.length; },
    (id) => { timers.delete(id); },
    (cb) => { const id = nextTimer; nextTimer += 1; timers.set(id, cb); return id; },
  );
  const flushRaf = () => { const q = rafQueue.splice(0); q.forEach((cb) => cb()); };
  const flushTimers = () => { const q = [...timers.values()]; timers.clear(); q.forEach((cb) => cb()); };
  const buttons = () => toastEl.children.filter((c) => c.tagName === 'button');
  return { api, state, toastEl, reloads, flushRaf, flushTimers, buttons };
}

const versionFetch = (version) => async () => ({ ok: true, json: async () => ({ version }) });

test('harness: a new version shows the toast; refresh reloads only on tap', async () => {
  const h = makeBehaviourHarness({ baseline: 'abc1234', fetchImpl: versionFetch('def5678') });
  await h.api.checkServerVersion();
  h.flushRaf();
  assert.equal(h.toastEl.hidden, false);
  assert.ok(h.toastEl.classList.contains('has-action'));
  assert.ok(h.toastEl.classList.contains('show'));
  assert.ok(h.toastEl.textContent.includes('服务端已更新'));
  assert.deepEqual(h.buttons().map((b) => b.textContent), ['立即刷新', '稍后']);
  assert.equal(h.reloads.length, 0, 'no reload before the user taps anything');
  h.buttons()[0].onclick();
  assert.equal(h.reloads.length, 1, 'tapping refresh reloads once');
});

test('harness: same version shows nothing', async () => {
  const h = makeBehaviourHarness({ baseline: 'abc1234', fetchImpl: versionFetch('abc1234') });
  await h.api.checkServerVersion();
  h.flushRaf();
  assert.equal(h.toastEl.hidden, true);
  assert.equal(h.reloads.length, 0);
});

test('harness: old server without the endpoint (404) fails silently', async () => {
  const h = makeBehaviourHarness({
    baseline: 'abc1234',
    fetchImpl: async () => ({ ok: false, status: 404 }),
  });
  await h.api.checkServerVersion();
  h.flushRaf();
  assert.equal(h.toastEl.hidden, true);
  assert.equal(h.reloads.length, 0);
});

test('harness: network failure fails silently', async () => {
  const h = makeBehaviourHarness({
    baseline: 'abc1234',
    fetchImpl: async () => { throw new Error('network down'); },
  });
  await h.api.checkServerVersion();
  h.flushRaf();
  assert.equal(h.toastEl.hidden, true);
  assert.equal(h.reloads.length, 0);
});

test('harness: dismiss silences this version, the next version prompts again', async () => {
  const h = makeBehaviourHarness({ baseline: 'abc1234', fetchImpl: versionFetch('def5678') });
  await h.api.checkServerVersion();
  h.flushRaf();
  assert.equal(h.toastEl.hidden, false);
  h.buttons()[1].onclick(); // 稍后
  h.flushTimers();
  assert.equal(h.toastEl.hidden, true);
  assert.equal(h.state.versionToast.dismissedFor, 'def5678');
  await h.api.checkServerVersion(); // same version again: stays silent
  h.flushRaf();
  assert.equal(h.toastEl.hidden, true);
  const h2 = makeBehaviourHarness({ baseline: 'abc1234', fetchImpl: versionFetch('aaa0000') });
  h2.state.versionToast.dismissedFor = 'def5678';
  await h2.api.checkServerVersion();
  h2.flushRaf();
  assert.equal(h2.toastEl.hidden, false, 'a newer version prompts again');
});

test('harness: a plain toast clobbering the prompt lets the next check re-show it', async () => {
  const h = makeBehaviourHarness({ baseline: 'abc1234', fetchImpl: versionFetch('def5678') });
  await h.api.checkServerVersion();
  h.flushRaf();
  assert.equal(h.toastEl.hidden, false);
  h.api.toast('已发送');
  assert.equal(h.state.versionToast.visible, false, 'plain toast took over the element');
  await h.api.checkServerVersion();
  h.flushRaf();
  assert.equal(h.toastEl.hidden, false, 'next check re-shows the version prompt');
  assert.ok(h.toastEl.textContent.includes('服务端已更新'));
});
