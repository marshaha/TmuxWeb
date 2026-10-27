// Unit tests for the optimistic-message reconciliation in web/index.html.
// Run with: node --test tests/js/
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

const build = (pending) => {
  const state = { selected: '%9', pendingMessages: new Map(Object.entries(pending)) };
  const source = grab('messageTextKey') + grab('reconcilePending') + 'return reconcilePending;';
  const reconcile = new Function('state', source)(state);
  return { state, reconcile };
};

const pending = (text, baseline = []) => ({
  pane: '%9', text, status: 'sent', baselineIds: new Set(baseline), createdAt: 1,
});

test('an exact echo retires the pending bubble', () => {
  const { state, reconcile } = build({ p1: pending('第一行\n第二行') });
  reconcile([{ id: 'a', role: 'user', text: '第一行\n第二行' }]);
  assert.equal(state.pendingMessages.size, 0);
});

test('whitespace and wrapping differences still reconcile', () => {
  const { state, reconcile } = build({ p1: pending('第一行\n第二行') });
  reconcile([{ id: 'a', role: 'user', text: '第一行   \n\n  第二行\n' }]);
  assert.equal(state.pendingMessages.size, 0);
});

test('a rewritten echo is paired by order, so nothing lingers', () => {
  const { state, reconcile } = build({ p1: pending('看看 log') });
  reconcile([{ id: 'a', role: 'user', text: '## 用户请求\n看看 log\n\n<environment_context>…' }]);
  assert.equal(state.pendingMessages.size, 0);
});

test('messages already in the transcript are not mistaken for the send', () => {
  const { state, reconcile } = build({ p1: pending('继续', ['old']) });
  reconcile([{ id: 'old', role: 'user', text: '继续' }]);
  assert.equal(state.pendingMessages.size, 1);
});

test('assistant chatter alone never clears a pending send', () => {
  const { state, reconcile } = build({ p1: pending('继续', ['old']) });
  reconcile([
    { id: 'old', role: 'user', text: '继续' },
    { id: 'b', role: 'assistant', text: '继续分析中…' },
  ]);
  assert.equal(state.pendingMessages.size, 1);
});

test('two sends retire one pending each, oldest first', () => {
  const first = { ...pending('一', []), createdAt: 1 };
  const second = { ...pending('二', []), createdAt: 2 };
  const { state, reconcile } = build({ p1: first, p2: second });
  reconcile([
    { id: 'x', role: 'user', text: '一' },
    { id: 'y', role: 'user', text: '二' },
  ]);
  assert.equal(state.pendingMessages.size, 0);
});

test('failed sends are kept so the error stays visible', () => {
  const { state, reconcile } = build({ p1: { ...pending('失败'), status: 'failed' } });
  reconcile([{ id: 'x', role: 'user', text: '失败' }]);
  assert.equal(state.pendingMessages.size, 1);
});

test('a pending from another pane is untouched', () => {
  const { state, reconcile } = build({ p1: { ...pending('别的'), pane: '%7' } });
  reconcile([{ id: 'x', role: 'user', text: '别的' }]);
  assert.equal(state.pendingMessages.size, 1);
});

test('a sent bubble anchors right after the transcript it was sent on', () => {
  const state = { selected: '%9', pendingMessages: new Map() };
  const anchor = new Function('state', grab('pendingAnchorIndex') + 'return pendingAnchorIndex;')(state);
  const messages = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
  assert.equal(anchor(messages, { baselineIds: new Set(['a']) }), 1);
  assert.equal(anchor(messages, { baselineIds: new Set(['a', 'b']) }), 2);
  // the agent answered before the echo arrived: the bubble stays where it was sent
  assert.equal(anchor([...messages, { id: 'd' }], { baselineIds: new Set(['a', 'b']) }), 2);
  assert.equal(anchor(messages, { baselineIds: new Set() }), 0);
});
