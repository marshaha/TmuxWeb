// Unit tests for the send-result display in web/share.html.
// Run with: node --test tests/js/share-resend-enter.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const page = fs.readFileSync(path.join(root, '..', '..', 'web', 'share.html'), 'utf8');

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

const inputResultText = new Function(grab('inputResultText') + 'return inputResultText;')();
const pendingClass = new Function(grab('pendingClass') + 'return pendingClass;')();

test('pasted_not_submitted shows the warning text', () => {
  assert.equal(inputResultText({ status: 'pasted_not_submitted', submitted: false }), '已粘贴但未提交');
});

test('sent shows the ok text', () => {
  assert.equal(inputResultText({ status: 'sent', submitted: true }), '已发送 ✓');
});

test('a missing result defaults to the sent text', () => {
  assert.equal(inputResultText(null), '已发送 ✓');
  assert.equal(inputResultText({}), '已发送 ✓');
});

test('the warning text renders with the fail class', () => {
  assert.equal(pendingClass('已粘贴但未提交'), 'fail');
  assert.equal(pendingClass('发送失败'), 'fail');
  assert.equal(pendingClass('已发送 ✓'), 'ok');
  assert.equal(pendingClass('发送中 …'), '');
});

test('sendMessage reads the response body instead of trusting HTTP 200', () => {
  const start = page.indexOf('  async function sendMessage()');
  assert.notEqual(start, -1, 'missing sendMessage');
  const body = page.slice(start, page.indexOf("  $('login-form')", start));
  assert.ok(body.includes('await r.json()'), 'sendMessage must parse the response body');
  assert.ok(body.includes('inputResultText('), 'sendMessage must map the result to a status text');
});
