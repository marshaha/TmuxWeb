// Unit tests for the incremental-transcript merge in web/index.html.
// Run with: node --test tests/js/chat-refresh.test.mjs
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

const merge = new Function(grab('mergeIncrementalMessages') + 'return mergeIncrementalMessages;')();
const msg = (id) => ({ id, role: 'assistant', text: id });

test('appends the new tail when the anchor is the last message', () => {
  const existing = [msg('m1'), msg('m2')];
  const merged = merge(existing, [msg('m3'), msg('m4')], 'm2');
  assert.deepEqual(merged.map((item) => item.id), ['m1', 'm2', 'm3', 'm4']);
  assert.deepEqual(existing.map((item) => item.id), ['m1', 'm2'], 'inputs are not mutated');
});

test('drops incoming ids that are already in the list', () => {
  const merged = merge([msg('m1'), msg('m2')], [msg('m2'), msg('m3')], 'm2');
  assert.deepEqual(merged.map((item) => item.id), ['m1', 'm2', 'm3']);
});

test('returns null when the anchor is not the last message', () => {
  assert.equal(merge([msg('m1'), msg('m2')], [msg('m3')], 'm1'), null);
  assert.equal(merge([msg('m1'), msg('m2')], [msg('m3')], 'gone'), null);
});

test('returns null when there is no local list to extend', () => {
  assert.equal(merge([], [msg('m1')], 'm1'), null);
  assert.equal(merge(null, [msg('m1')], 'm1'), null);
});
