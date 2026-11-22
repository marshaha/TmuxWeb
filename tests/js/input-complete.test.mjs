// Unit tests for the borrowed input completion in web/assets/console-app.js
// (Open WebUI MessageInput.svelte: @ mention + / slash-command suggestions,
// CommandSuggestionList.svelte: '/' -> SlashCommands, '@' -> AtCommands).
// Only the interaction is borrowed: the textarea stays plain text, the
// completion only deletes a token, inserts plain text, or switches pane;
// sendInput is untouched and the text goes to tmux verbatim.
// Run with: node --test tests/js/input-complete.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const page = fs.readFileSync(path.join(root, '..', '..', 'web', 'assets', 'console-app.js'), 'utf8');
const index = fs.readFileSync(path.join(root, '..', '..', 'web', 'index.html'), 'utf8');

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

const pureSrc = [
  grab('matchInputCompletion'),
  grab('filterPaneCompletions'),
  grab('filterCommandCompletions'),
  grab('applyCompletion'),
  grab('inputCommands'),
  grab('paneLabel'),
  grab('esc'),
].join('\n');
const pure = new Function(
  pureSrc + '\nreturn {matchInputCompletion,filterPaneCompletions,filterCommandCompletions,applyCompletion,inputCommands};',
)();
const { matchInputCompletion, filterPaneCompletions, filterCommandCompletions, applyCompletion, inputCommands } = pure;

test('match: @ at the start opens a pane completion', () => {
  assert.deepEqual(matchInputCompletion('@', 1), { kind: 'pane', start: 0, query: '' });
  assert.deepEqual(matchInputCompletion('hi @cl', 6), { kind: 'pane', start: 3, query: 'cl' });
  assert.deepEqual(matchInputCompletion('line1\n@x', 8), { kind: 'pane', start: 6, query: 'x' });
});

test('match: / at the start opens a command completion', () => {
  assert.deepEqual(matchInputCompletion('/z', 2), { kind: 'command', start: 0, query: 'z' });
  assert.deepEqual(matchInputCompletion('do /jixu', 8), { kind: 'command', start: 3, query: 'jixu' });
});

test('match: @ and / inside a word do not trigger (email, URL, arithmetic)', () => {
  assert.equal(matchInputCompletion('a@b', 3), null);
  assert.equal(matchInputCompletion('http://x', 8), null);
  assert.equal(matchInputCompletion('3/4', 3), null);
  assert.equal(matchInputCompletion('/x/y', 4), null);
});

test('match: a space after the word abandons the completion', () => {
  assert.equal(matchInputCompletion('foo @bar ', 9), null);
  assert.equal(matchInputCompletion('foo /z ', 7), null);
});

test('match: only the word under the cursor counts', () => {
  assert.deepEqual(matchInputCompletion('foo @bar baz', 8), { kind: 'pane', start: 4, query: 'bar' });
  assert.equal(matchInputCompletion('foo @bar baz', 12), null);
});

const PANES = [
  { id: '%1', title: 'claude 助手', target: '%1', command: 'claude', number: 1 },
  { id: '%2', title: 'codex 任务', target: '%2', command: 'codex', number: 2 },
  { id: '%3', title: 'shell', target: '%3', command: 'zsh', number: 3 },
  { id: null, title: 'ghost', target: '?', command: 'x', number: 9 },
];

test('filterPaneCompletions: empty query lists every pane, capped at 8', () => {
  const many = Array.from({ length: 10 }, (_, i) => ({ id: `%${i}`, title: `p${i}`, number: i }));
  assert.equal(filterPaneCompletions(many, '').length, 8);
  assert.deepEqual(filterPaneCompletions(PANES, '').map((p) => p.id), ['%1', '%2', '%3']);
});

test('filterPaneCompletions: matches title, command, number and #number', () => {
  assert.deepEqual(filterPaneCompletions(PANES, 'claude').map((p) => p.id), ['%1']);
  assert.deepEqual(filterPaneCompletions(PANES, 'codex').map((p) => p.id), ['%2']);
  assert.deepEqual(filterPaneCompletions(PANES, '#3').map((p) => p.id), ['%3']);
  assert.deepEqual(filterPaneCompletions(PANES, 'CLAUDE').map((p) => p.id), ['%1']);
  assert.deepEqual(filterPaneCompletions(PANES, '不存在'), []);
});

test('filterCommandCompletions: six built-in commands, matched by label or pinyin key', () => {
  assert.equal(filterCommandCompletions('').length, 6);
  assert.deepEqual(filterCommandCompletions('总结').map((c) => c.label), ['总结']);
  assert.deepEqual(filterCommandCompletions('jixu').map((c) => c.label), ['继续']);
  assert.deepEqual(filterCommandCompletions('z').map((c) => c.label), ['总结', '自查']);
  assert.deepEqual(filterCommandCompletions('不存在'), []);
});

test('inputCommands: every command inserts plain text only, nothing auto-sends', () => {
  for (const c of inputCommands()) {
    assert.equal(typeof c.text, 'string');
    assert.ok(c.text.length > 0);
    assert.equal(c.text.includes('<'), false);
  }
});

test('applyCompletion: replaces only the token, keeps the rest, moves the caret', () => {
  assert.deepEqual(
    applyCompletion('hello @cl', 9, { kind: 'pane', start: 6, query: 'cl' }, ''),
    { text: 'hello ', cursor: 6 },
  );
  assert.deepEqual(
    applyCompletion('/总结', 3, { kind: 'command', start: 0, query: '总结' }, '总结一下目前的进展'),
    { text: '总结一下目前的进展', cursor: 9 },
  );
  assert.deepEqual(
    applyCompletion('请 /总结 之后', 5, { kind: 'command', start: 2, query: '总结' }, '总结一下'),
    { text: '请 总结一下 之后', cursor: 6 },
  );
});

// ---- wiring: acceptInputComplete with stubbed DOM/state ----

const wiringSrc = [
  'let completeMatch=null,completeItems=[],completeIndex=0,completeOpen=false,completeSuppressed=false;',
  grab('matchInputCompletion'),
  grab('filterPaneCompletions'),
  grab('filterCommandCompletions'),
  grab('applyCompletion'),
  grab('inputCommands'),
  grab('paneLabel'),
  grab('esc'),
  grab('closeInputComplete'),
  grab('paintCompleteSelection'),
  grab('completeMove'),
  grab('renderInputComplete'),
  grab('refreshInputComplete'),
  grab('acceptInputComplete'),
].join('\n');

function makeHarness({ panes, selected }) {
  const calls = { selectPane: [] };
  const dispatched = [];
  const box = { hidden: true, innerHTML: '', querySelectorAll: () => [], addEventListener() {} };
  const state = { selected, panes, drafts: new Map() };
  const input = {
    value: '',
    selectionStart: 0,
    dispatchEvent(e) { dispatched.push(e.type); },
    setSelectionRange(a, b) { this._sel = [a, b]; },
    focus() { this._focused = true; },
  };
  const api = new Function(
    'state', 'input', 'selectPane', 'isMobile', '$',
    wiringSrc + '\nreturn {acceptInputComplete,refreshInputComplete,completeState:()=>({completeMatch,completeItems,completeIndex,completeOpen})};',
  )(state, input, (id) => calls.selectPane.push(id), () => false, (id) => (id === 'input-complete' ? box : null));
  return { api, state, input, box, calls, dispatched };
}

test('accept pane: removes the @token from the old draft and switches pane', () => {
  const h = makeHarness({ panes: PANES, selected: '%3' });
  h.input.value = '@cl';
  h.input.selectionStart = 3;
  h.api.refreshInputComplete(true);
  const st = h.api.completeState();
  assert.equal(st.completeOpen, true);
  assert.equal(st.completeItems[0].id, '%1');
  h.api.acceptInputComplete(st.completeItems[0]);
  assert.deepEqual(h.calls.selectPane, ['%1']);
  assert.equal(h.state.drafts.get('%3'), '');
  assert.equal(h.input.value, '');
  assert.equal(h.input._focused, true);
});

test('accept pane: picking the current pane only cleans the token, no reselect', () => {
  const h = makeHarness({ panes: PANES, selected: '%1' });
  h.input.value = 'hi @cl';
  h.input.selectionStart = 6;
  h.api.refreshInputComplete(true);
  const st = h.api.completeState();
  assert.equal(st.completeItems[0].id, '%1');
  h.api.acceptInputComplete(st.completeItems[0]);
  assert.deepEqual(h.calls.selectPane, []);
  assert.equal(h.input.value, 'hi ');
});

test('accept command: inserts the preset text, does not send', () => {
  const h = makeHarness({ panes: PANES, selected: '%1' });
  h.input.value = '/总结';
  h.input.selectionStart = 3;
  h.api.refreshInputComplete(true);
  const st = h.api.completeState();
  assert.equal(st.completeItems[0].label, '总结');
  h.api.acceptInputComplete(st.completeItems[0]);
  assert.deepEqual(h.calls.selectPane, []);
  assert.equal(h.input.value, '总结一下目前的进展：已完成什么、正在做什么、还剩什么');
  assert.deepEqual(h.input._sel, [h.input.value.length, h.input.value.length]);
  assert.ok(h.dispatched.includes('input'));
});

test('accept: the completion never calls the send path', () => {
  const body = grab('acceptInputComplete');
  assert.equal(body.includes('sendInput('), false);
  assert.equal(body.includes('api('), false);
});

// ---- message actions: resend button ----

const clockSource = page.slice(page.indexOf('  const clockTime='), page.indexOf('  function transcriptBubble('));
const bubbleSrc = [clockSource, grab('esc'), grab('msgResendButton'), grab('transcriptBubble')].join('\n');
const bubble = new Function(
  'markdown', 'userBubbleHtml',
  bubbleSrc + '\nreturn transcriptBubble;',
)((text) => `<p>${text}</p>`, (message) => message.text);

test('transcriptBubble: user messages get a resend button, assistant messages do not', () => {
  const user = bubble({ id: 'm1', role: 'user', text: 'hello' });
  assert.ok(user.includes('data-msg-resend="m1"'));
  assert.ok(user.includes('再次发送'));
  assert.ok(user.includes('data-msg-copy="m1"'));
  const asst = bubble({ id: 'm2', role: 'assistant', text: 'hi' });
  assert.equal(asst.includes('data-msg-resend'), false);
  assert.ok(asst.includes('data-msg-copy="m2"'));
});

// ---- wiring is present in the page ----

test('composer wires the completion popup and the resend action', () => {
  assert.ok(index.includes('id="input-complete"'));
  assert.ok(index.includes('@ 跳 pane · / 插指令'));
  assert.ok(index.includes('.input-complete{'));
  assert.ok(page.includes('data-msg-resend'));
  assert.ok(page.includes("box.addEventListener('mousedown'"));
  assert.ok(page.includes('refreshInputComplete(true)'));
  // Red line: no rich-text editor. The textarea stays plain text.
  // (The word Tiptap appears once in a comment stating exactly that.)
  assert.equal(page.includes('@tiptap'), false);
  assert.equal(page.includes('RichTextInput'), false);
});
