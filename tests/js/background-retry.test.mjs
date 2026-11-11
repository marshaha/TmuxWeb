// Unit tests for the background-retry display in web/index.html and web/share.html.
// Run with: node --test tests/js/background-retry.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const indexPage = fs.readFileSync(path.join(root, '..', '..', 'web', 'index.html'), 'utf8');
const sharePage = fs.readFileSync(path.join(root, '..', '..', 'web', 'share.html'), 'utf8');

function grab(page, name) {
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

const sendResultState = new Function(grab(indexPage, 'sendResultState') + 'return sendResultState;')();
const deliveryHtml = new Function(grab(indexPage, 'deliveryHtml') + 'return deliveryHtml;')();
// deliveryHtml escapes the local id with the page's esc helper.
globalThis.esc = (s) => String(s == null ? '' : s);
const resendAction = new Function(grab(indexPage, 'resendAction') + 'return resendAction;')();
const syncResendState = new Function(grab(indexPage, 'syncResendState') + 'return syncResendState;')();
const shareInputResultText = new Function(grab(sharePage, 'inputResultText') + 'return inputResultText;')();
const sharePendingClass = new Function(grab(sharePage, 'pendingClass') + 'return pendingClass;')();
const shareResendAction = new Function(grab(sharePage, 'resendAction') + 'return resendAction;')();
const shareApplyResend = new Function(grab(sharePage, 'applyResend') + 'return applyResend;')();

test('a retrying send shows 正在重试提交… in the send bar, not 已发送', () => {
  assert.deepEqual(
    sendResultState({ status: 'retrying', submitted: false, retrying: true }),
    { text: '正在重试提交…', kind: 'retrying' });
  assert.deepEqual(
    sendResultState({ status: 'pasted_not_submitted', submitted: false }),
    { text: '已粘贴但未提交', kind: 'bad' });
  assert.deepEqual(sendResultState({ status: 'sent', submitted: true }), { text: '已发送 ✓', kind: 'ok' });
});

test('a retrying bubble shows the wave and no resend button', () => {
  const html = deliveryHtml({ status: 'retrying', text: 'hello' }, 'local-1');
  assert.match(html, /正在重试提交…/);
  assert.doesNotMatch(html, /重发/);
  const failed = deliveryHtml({ status: 'failed', text: 'hello' }, 'local-1');
  assert.match(failed, /重发/);
});

test('resendAction maps the loop outcome to an optimistic status', () => {
  assert.equal(resendAction(null), null);
  assert.equal(resendAction({}), null);
  assert.equal(resendAction({ status: 'retrying' }), null);
  assert.equal(resendAction({ status: 'submitted' }), 'sent');
  assert.equal(resendAction({ status: 'failed' }), 'failed');
  assert.equal(resendAction({ status: 'superseded' }), 'failed');
});

test('syncResendState flips a retrying bubble when the loop finishes', () => {
  const calls = [];
  globalThis.state = { selected: '%4', pendingMessages: new Map([
    ['l1', { pane: '%4', text: 'hi', status: 'retrying' }],
    ['l2', { pane: '%4', text: 'other', status: 'sent' }],
  ]) };
  globalThis.resendAction = resendAction;
  globalThis.setOptimisticStatus = (id, status) => calls.push([id, status]);
  globalThis.setSendState = () => {};
  try {
    syncResendState({ resend: { status: 'failed', presses: 12 } });
    assert.deepEqual(calls, [['l1', 'failed']]);
    syncResendState({ resend: { status: 'retrying', presses: 3 } });
    assert.deepEqual(calls, [['l1', 'failed']]);
    syncResendState({ resend: { status: 'submitted', presses: 2 } });
    assert.deepEqual(calls, [['l1', 'failed'], ['l1', 'sent']]);
  } finally {
    delete globalThis.state;
    delete globalThis.resendAction;
    delete globalThis.setOptimisticStatus;
    delete globalThis.setSendState;
  }
});

test('refreshTranscript applies the resend state before the unchanged early return', () => {
  const start = indexPage.indexOf('  async function refreshTranscript(');
  assert.notEqual(start, -1, 'missing refreshTranscript');
  const body = indexPage.slice(start, indexPage.indexOf('  let tailTimer', start));
  const syncAt = body.indexOf('syncResendState(d)');
  assert.notEqual(syncAt, -1, 'refreshTranscript must call syncResendState');
  assert.ok(syncAt < body.indexOf('if(d.unchanged)'), 'resend sync must run before the unchanged return');
});

test('sendInput and resendPending keep a retrying bubble in the retrying state', () => {
  for (const name of ['async function sendInput()', 'async function resendPending(']) {
    const start = indexPage.indexOf('  ' + name);
    assert.notEqual(start, -1, `missing ${name}`);
    const body = indexPage.slice(start, indexPage.indexOf('\n  }', start));
    assert.ok(body.includes("'retrying'"), `${name} must map the retrying result to a retrying bubble`);
  }
});

test('share inputResultText shows 正在重试提交… for a retrying send', () => {
  assert.equal(shareInputResultText({ status: 'retrying', submitted: false, retrying: true }), '正在重试提交…');
  assert.equal(shareInputResultText({ status: 'pasted_not_submitted', submitted: false }), '已粘贴但未提交');
  assert.equal(shareInputResultText({ status: 'sent', submitted: true }), '已发送 ✓');
});

test('share pendingClass leaves the retrying text unstyled', () => {
  assert.equal(sharePendingClass('正在重试提交…'), '');
  assert.equal(sharePendingClass('已粘贴但未提交'), 'fail');
});

test('share resendAction matches the chat page mapping', () => {
  assert.equal(shareResendAction({ status: 'retrying' }), null);
  assert.equal(shareResendAction({ status: 'submitted' }), 'sent');
  assert.equal(shareResendAction({ status: 'failed' }), 'failed');
  assert.equal(shareResendAction({ status: 'superseded' }), 'failed');
});

test('share applyResend flips retrying pending items and re-renders', () => {
  const rendered = [];
  globalThis.pending = [{ text: 'hi', status: '正在重试提交…' }];
  globalThis.resendAction = shareResendAction;
  globalThis.render = (list) => rendered.push(list);
  try {
    shareApplyResend({ mode: 'transcript', messages: [{ id: 'u1' }], resend: { status: 'failed', presses: 4 } });
    assert.equal(globalThis.pending[0].status, '已粘贴但未提交');
    assert.equal(rendered.length, 1);
    shareApplyResend({ mode: 'transcript', messages: [], resend: { status: 'retrying', presses: 1 } });
    assert.equal(rendered.length, 1, 'still retrying: nothing to update');
  } finally {
    delete globalThis.pending;
    delete globalThis.resendAction;
    delete globalThis.render;
  }
});

test('share refresh applies the resend state on every poll, not only on mtime change', () => {
  const start = sharePage.indexOf('  async function refresh()');
  assert.notEqual(start, -1, 'missing refresh');
  const body = sharePage.slice(start, sharePage.indexOf('  function inputResultText', start));
  const applyAt = body.indexOf('applyResend(d)');
  assert.notEqual(applyAt, -1, 'refresh must call applyResend');
  assert.ok(applyAt < body.indexOf("if(d.mtime!==lastMtime)"), 'resend sync must run before the mtime filter');
});
