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
