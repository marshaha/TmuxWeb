// Unit tests for the borrowed content-visibility virtualization in
// web/assets/console-app.js (Open WebUI Message.svelte: message-virtualized).
// Run with: node --test tests/js/content-visibility.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const page = fs.readFileSync(path.join(root, '..', '..', 'web', 'assets', 'console-app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, '..', '..', 'web', 'assets', 'console-base.css'), 'utf8');

function grab(name) {
  const start = page.indexOf('  function ' + name + '(');
  assert.notEqual(start, -1, `missing function ${name}`);
  let depth = 0;
  for (let i = page.indexOf('{', start); i < page.length; i += 1) {
    if (page[i] === '{') depth += 1;
    else if (page[i] === '}') {
      depth -= 1;
      if (!depth) return page.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced ${name}`);
}

const src = [grab('isWebKit'), grab('virtualizedClass'), grab('esc'), grab('msgResendButton'), grab('transcriptBubble')].join('\n');
const virtualizedClass = new Function(src + '\nreturn virtualizedClass;')();
const isWebKit = new Function(src + '\nreturn isWebKit;')();
const bubble = new Function(
  'markdown', 'userBubbleHtml',
  src + '\nreturn transcriptBubble;',
)((text) => `<p>${text}</p>`, (message) => message.text);

function setNavigator(vendor, userAgent) {
  // Node 24 exposes globalThis.navigator as a getter-only property.
  Object.defineProperty(globalThis, 'navigator', { value: { vendor, userAgent }, configurable: true });
}

const CHROME = { vendor: 'Google Inc.', userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36' };
const SAFARI = { vendor: 'Apple Computer, Inc.', userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15' };
// iOS WebView / home screen app: no Safari token, Apple vendor only.
const IOS_WEBVIEW = { vendor: 'Apple Computer, Inc.', userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148' };

test('off-screen messages get the class, the last (current turn) does not', () => {
  setNavigator(CHROME.vendor, CHROME.userAgent);
  assert.equal(virtualizedClass(0, 5), ' msg-virtualized');
  assert.equal(virtualizedClass(3, 5), ' msg-virtualized');
  assert.equal(virtualizedClass(4, 5), '', 'last message is the current turn: streaming lands on it');
  assert.equal(virtualizedClass(0, 1), '', 'a lone message is the current turn');
});

test('WebKit never virtualizes (upstream paint bug #26712)', () => {
  for (const ua of [SAFARI, IOS_WEBVIEW]) {
    setNavigator(ua.vendor, ua.userAgent);
    assert.equal(isWebKit(), true, ua.userAgent);
    assert.equal(virtualizedClass(0, 5), '');
    assert.equal(virtualizedClass(0, 1), '');
  }
  setNavigator(CHROME.vendor, CHROME.userAgent);
  assert.equal(isWebKit(), false);
});

test('transcriptBubble stamps the class on older bubbles only', () => {
  setNavigator(CHROME.vendor, CHROME.userAgent);
  const user = { id: 'u1', role: 'user', text: 'hi' };
  const asst = { id: 'a1', role: 'assistant', text: 'hello' };
  assert.match(bubble(user, 0, 3), /class="chat-message user msg-virtualized"/);
  assert.match(bubble(asst, 1, 3), /class="chat-message assistant msg-virtualized"/);
  assert.match(bubble(asst, 2, 3), /class="chat-message assistant"/);
  assert.doesNotMatch(bubble(asst, 2, 3), /msg-virtualized/);
  setNavigator(SAFARI.vendor, SAFARI.userAgent);
  assert.doesNotMatch(bubble(user, 0, 3), /msg-virtualized/);
});

test('the CSS rule is exactly the borrowed one-liner plus intrinsic size', () => {
  assert.match(css, /\.chat-message\.msg-virtualized\{content-visibility:auto;contain-intrinsic-size:auto 150px\}/);
});

test('both render paths wire the class; pending bubbles never get it', () => {
  // Full rebuild path (formal map) and append-only path.
  assert.ok(page.includes('virtualizedClass(index,messages.length)'), 'formal map must pass the index');
  assert.ok(page.includes('transcriptBubble(message,rendered.length+offset,messages.length)'), 'append path must pass the index');
  // Pending (optimistic) bubbles are built from a separate template without the class.
  const pendingAt = page.indexOf('<article class="chat-message user pending"');
  assert.notEqual(pendingAt, -1, 'pending template must stay class-free');
  const pendingLine = page.slice(pendingAt, page.indexOf('\n', pendingAt));
  assert.ok(!pendingLine.includes('msg-virtualized') && !pendingLine.includes('virtualizedClass'),
    'pending template must not gain the class');
});

test('streaming updates in place instead of rebuilding (keeps remembered sizes)', () => {
  // A full innerHTML rebuild on every streaming poll would destroy every
  // bubble, and with content-visibility that also destroys every remembered
  // intrinsic size, so scrollHeight collapses and a reader scrolled up loses
  // their place. Same ids + text-only change must update just those bubbles.
  assert.ok(page.includes('const prevText=state.messageText||new Map();'), 'old texts must be kept for the diff');
  assert.ok(page.includes('if(!pending.length&&rendered.length===ids.length'), 'in-place path needs same ids, no pending');
  assert.ok(page.includes('node.replaceWith(...wrap.childNodes)'), 'only the changed bubble is replaced');
  assert.ok(page.includes('enhancePathsLazy(host,true)'), 'in-place path re-enhances like the rebuild');
});

test('load-earlier anchors on the visible message, not on scrollHeight', () => {
  // A scrollHeight delta is an estimate under content-visibility (and a
  // rebuild forgets every remembered size, so the delta can go negative and
  // throw the reader to the top). The anchor's own position is measured live.
  assert.ok(page.includes('function firstVisibleMessage()'), 'anchor helper must exist');
  assert.ok(page.includes('function restoreAnchor(anchor)'), 'restore helper must exist');
  assert.ok(page.includes('const previous=state.transcriptMessages.length,anchor=firstVisibleMessage();'),
    'loadEarlier must capture the anchor before fetching');
  assert.ok(page.includes('requestAnimationFrame(()=>restoreAnchor(anchor));'),
    'anchor is corrected again one frame later, after neighbours are measured');
  assert.ok(!page.includes('scroll.scrollTop+=scroll.scrollHeight-height'),
    'the scrollHeight-delta compensation must be gone');
});
