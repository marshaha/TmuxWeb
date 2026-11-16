// Unit tests for the borrowed rAF-throttled streaming transcript render in
// web/assets/console-app.js (Open WebUI
// src/lib/components/chat/Messages/Markdown.svelte:121: while streaming,
// marked parses at most once per animation frame; when done it parses
// immediately; the pending frame is cancelled on destroy).
// Run with: node --test tests/js/raf-throttle.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const page = fs.readFileSync(path.join(root, '..', '..', 'web', 'assets', 'console-app.js'), 'utf8');

function grab(name) {
  const start = page.indexOf('function ' + name + '(');
  assert.notEqual(start, -1, `missing function ${name}`);
  // Skip the parameter list first: it may itself contain braces (e.g. the
  // destructuring default in refreshTranscript's {force,refresh} options),
  // which a naive brace count would mistake for the body open brace.
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

const declStart = page.indexOf('let transcriptRenderFrame=null, transcriptRenderPending=null;');
assert.notEqual(declStart, -1, 'missing scheduler state');
const decl = page.slice(declStart, page.indexOf('\n', declStart));
const schedulerSrc = [decl, grab('cancelScheduledTranscriptRender'), grab('renderTranscriptNow'),
  grab('scheduleTranscriptRender'), grab('applyTranscriptUpdate')].join('\n');

function makeHarness() {
  const renders = [];
  const scroll = { scrollTop: 0, scrollHeight: 500 };
  const state = { selected: 'p1', mode: 'chat' };
  let nextId = 1;
  let rafQueue = [];
  const requestAnimationFrame = (cb) => {
    const id = nextId;
    nextId += 1;
    rafQueue.push({ id, cb });
    return id;
  };
  const cancelAnimationFrame = (id) => { rafQueue = rafQueue.filter((entry) => entry.id !== id); };
  const api = new Function('renderTranscript', 'state', 'scroll',
    'requestAnimationFrame', 'cancelAnimationFrame',
    schedulerSrc + '\nreturn {scheduleTranscriptRender, renderTranscriptNow,'
    + ' cancelScheduledTranscriptRender, applyTranscriptUpdate};')(
    (messages) => { renders.push(messages); }, state, scroll,
    requestAnimationFrame, cancelAnimationFrame);
  const fireFrames = () => {
    const queued = rafQueue;
    rafQueue = [];
    queued.forEach((entry) => entry.cb());
  };
  return { api, renders, scroll, state, fireFrames, pendingFrames: () => rafQueue.length };
}

const M = (id, text) => ({ id, role: 'assistant', text });

test('a burst inside one frame renders once, with the latest messages', () => {
  const h = makeHarness();
  for (let i = 1; i <= 5; i += 1) {
    h.api.scheduleTranscriptRender('p1', [M('a', 'x'.repeat(i * 200))], null);
  }
  assert.equal(h.pendingFrames(), 1, 'one frame for the whole burst');
  assert.equal(h.renders.length, 0, 'nothing renders before the frame');
  h.fireFrames();
  assert.equal(h.renders.length, 1);
  assert.equal(h.renders[0][0].text.length, 1000, 'the last update wins');
  assert.equal(h.pendingFrames(), 0);
});

test('the done update renders immediately, like upstream\'s done branch', () => {
  const h = makeHarness();
  h.api.scheduleTranscriptRender('p1', [M('a', 'streaming')], null);
  h.api.applyTranscriptUpdate('p1', [M('a', 'final')], true, true);
  assert.equal(h.renders.length, 1, 'done renders synchronously');
  assert.equal(h.renders[0][0].text, 'final');
  assert.equal(h.pendingFrames(), 0, 'the pending streaming frame was cancelled');
  h.fireFrames();
  assert.equal(h.renders.length, 1, 'no second render from the cancelled frame');
  assert.equal(h.scroll.scrollTop, h.scroll.scrollHeight, 'follow-bottom scroll ran with the render');
});

test('a streaming update renders on the next frame, scroll after the DOM write', () => {
  const h = makeHarness();
  h.api.applyTranscriptUpdate('p1', [M('a', 'growing')], true, false);
  assert.equal(h.renders.length, 0, 'streaming waits for the frame');
  assert.equal(h.scroll.scrollTop, 0, 'no scroll before the render');
  h.fireFrames();
  assert.equal(h.renders.length, 1);
  assert.equal(h.scroll.scrollTop, 500, 'scroll runs in the same frame, after the render');
});

test('no follow-bottom, no scroll', () => {
  const h = makeHarness();
  h.api.applyTranscriptUpdate('p1', [M('a', 'growing')], false, false);
  h.fireFrames();
  assert.equal(h.renders.length, 1);
  assert.equal(h.scroll.scrollTop, 0);
});

test('a stale frame never paints over a new pane or a mode switch', () => {
  const h = makeHarness();
  h.api.scheduleTranscriptRender('p1', [M('a', 'old pane')], null);
  h.state.selected = 'p2';
  h.fireFrames();
  assert.equal(h.renders.length, 0, 'pane switch drops the pending render');

  h.api.scheduleTranscriptRender('p2', [M('a', 'new pane')], null);
  h.state.mode = 'terminal';
  h.fireFrames();
  assert.equal(h.renders.length, 0, 'leaving chat mode drops the pending render');
});

test('renderTranscriptNow cancels the pending frame and renders at once', () => {
  const h = makeHarness();
  h.api.scheduleTranscriptRender('p1', [M('a', 'stale')], null);
  h.api.renderTranscriptNow([M('a', 'fresh')]);
  assert.equal(h.renders.length, 1);
  assert.equal(h.renders[0][0].text, 'fresh');
  h.fireFrames();
  assert.equal(h.renders.length, 1, 'the cancelled frame renders nothing');
});

test('the poll path throttles, the immediate paths do not', () => {
  const refresh = grab('refreshTranscript');
  assert.ok(refresh.includes('applyTranscriptUpdate(id,merged,follow||first,!d.waiting)'),
    'incremental poll path must throttle, done (!waiting) flushes');
  assert.ok(refresh.includes('applyTranscriptUpdate(id,d.messages,follow||first,!d.waiting)'),
    'full poll path must throttle, done (!waiting) flushes');
  assert.ok(!refresh.includes('renderTranscript(merged)'), 'no direct render left on the incremental path');
  assert.ok(!refresh.includes('renderTranscript(d.messages)'), 'no direct render left on the full path');
  // The old scroll rAF after the render is gone: the scroll now rides the
  // scheduled frame, so it can no longer run ahead of the DOM write.
  assert.ok(!refresh.includes("requestAnimationFrame(()=>scroll.scrollTop=scroll.scrollHeight)"),
    'scroll must not be scheduled separately from the throttled render');
  for (const name of ['loadEarlier', 'paintCachedTranscript', 'addOptimisticMessage',
    'setOptimisticStatus', 'connectTranscript']) {
    assert.ok(grab(name).includes('renderTranscriptNow('),
      `${name} must render immediately (it is not the streaming path)`);
  }
  assert.ok(grab('closeConnections').includes('cancelScheduledTranscriptRender()'),
    'pane switch / mode change must cancel the pending frame');
});

test('the throttle changes timing only: render internals are untouched', () => {
  // On this base (origin/main 3e4761d) there is no in-place streaming path
  // and no content-visibility wiring yet — both live on the item-1 branch
  // web/borrow-content-visibility (PR #29, unmerged). The throttle must
  // therefore not depend on any render internals: it only decides WHEN
  // renderTranscript runs, so it composes with item 1 whichever merges first.
  const render = grab('renderTranscript');
  assert.ok(render.includes('const canAppend='), 'append-only path intact');
  assert.ok(render.includes('host.innerHTML=ordered'), 'full rebuild path intact');
  const scheduled = grab('scheduleTranscriptRender');
  assert.ok(scheduled.includes('renderTranscript(pending.messages)'),
    'the frame callback renders exactly what the latest update carried');
  assert.ok(!scheduled.includes('markdown(') && !scheduled.includes('innerHTML'),
    'the scheduler does no rendering of its own');
  assert.ok(page.includes('Markdown.svelte:121'), 'borrow source must be cited');
  assert.ok(page.includes('streamLargeDeltasAsRandomChunks'),
    'the deliberately-not-taken random-chunk half must be named');
  assert.ok(page.includes('function scheduleTranscriptRender('), 'scheduler must exist');
  assert.ok(page.includes('cancelAnimationFrame(transcriptRenderFrame)'), 'scheduler must cancel the frame');
});
