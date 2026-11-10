// Unit tests for the send-result display in web/index.html.
// Run with: node --test tests/js/resend-enter.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const page = fs.readFileSync(path.join(root, '..', '..', 'web', 'index.html'), 'utf8');

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

const stateFor = new Function(grab('sendResultState') + 'return sendResultState;')();

test('pasted_not_submitted shows the warning state', () => {
  assert.deepEqual(stateFor({ status: 'pasted_not_submitted', submitted: false }),
    { text: '已粘贴但未提交', kind: 'bad' });
});

test('sent shows the ok state', () => {
  assert.deepEqual(stateFor({ status: 'sent', submitted: true }), { text: '已发送 ✓', kind: 'ok' });
});

test('a missing result defaults to the sent state', () => {
  assert.deepEqual(stateFor(null), { text: '已发送 ✓', kind: 'ok' });
  assert.deepEqual(stateFor({}), { text: '已发送 ✓', kind: 'ok' });
});
