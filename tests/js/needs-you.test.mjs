// Unit tests for the "needs you" console card (web/assets/needs-you.js).
// Run with: node --test tests/js/needs-you.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.dirname(new URL(import.meta.url).pathname);
const source = fs.readFileSync(
  path.join(root, '..', '..', 'web', 'assets', 'needs-you.js'), 'utf8');

function load() {
  const sandbox = {window: {}, document: {hidden: false, addEventListener() {}}, location: {}};
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, {filename: 'needs-you.js'});
  return sandbox.TmuxNeedsYou;
}

const needsYou = load();

test('needs-input panes sort first, the rest keep their order', () => {
  const ids = (panes, wanted) => Array.from(needsYou.sortNeedsFirst(panes, wanted), pane => pane.id);
  const panes = [{id: '%4'}, {id: '%5'}, {id: '%6'}];
  assert.deepEqual(ids(panes, new Set(['%6'])), ['%6', '%4', '%5']);
  assert.deepEqual(ids(panes, ['%6', '%5']), ['%5', '%6', '%4'],
    'needs panes come first, in their original order — the set is not an ordering');
  assert.deepEqual(ids(panes, new Set()), ['%4', '%5', '%6']);
  // the input is not mutated: the tree/list render from the same array again
  assert.deepEqual(Array.from(panes, pane => pane.id), ['%4', '%5', '%6']);
});

test('the prompt signature changes when the menu changes', () => {
  const menu = {kind: 'menu', title: 'Do you want to proceed?', selected: 0,
    options: [{label: 'Yes'}, {label: 'No'}], toggles: []};
  const moved = {...menu, selected: 1};
  const relabelled = {...menu, options: [{label: 'Yes'}, {label: 'Nope'}]};
  assert.equal(needsYou.signatureOf(menu), needsYou.signatureOf({...menu}));
  assert.notEqual(needsYou.signatureOf(menu), needsYou.signatureOf(moved));
  assert.notEqual(needsYou.signatureOf(menu), needsYou.signatureOf(relabelled));
  assert.equal(needsYou.signatureOf(null), '');
});

test('the card answers only through /api/panes/{id}/key', () => {
  assert.match(source, /\/api\/panes\/\$\{encodeURIComponent\(pane\)\}\/key/);
  assert.doesNotMatch(source, /prompt\/choose/);
  assert.doesNotMatch(source, /\/api\/panes\/.*\/input/);
});
