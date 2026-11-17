// Unit tests for the sidebar pane search in web/assets/console-app.js:
// a local, instant filter over the loaded pane tree (no API call, no
// transcript content search). Run with: node --test tests/js/sidebar-search.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const page = fs.readFileSync(path.join(root, '..', '..', 'web', 'assets', 'console-app.js'), 'utf8');

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

const { parsePaneSearchTerms, paneMatchesSearch, filterHostsForSearch } = new Function(
  grab('parsePaneSearchTerms')
  + grab('paneSearchHaystack')
  + grab('paneMatchesSearch')
  + grab('filterHostsForSearch')
  + 'return {parsePaneSearchTerms, paneMatchesSearch, filterHostsForSearch};',
)();

const MACMINI = { id: '5ae77f56bbb13200', name: 'macmini' };
const LOCAL = { id: 'localhost', name: 'localhost' };

const HERMES_39 = {
  id: '5ae77f56bbb13200~%143', number: 39, command: 'hermes', provider: 'hermes',
  title: 'hermes', target: 'hermes:0.0', path: '/Users/marscrazy/workspace/hermes',
  session: 'hermes', window: '0', window_name: 'hermes', index: '143',
};
const KIMI_42 = {
  id: '5ae77f56bbb13200~%0', number: 42, command: 'kimi', provider: 'kimi',
  title: '阅读 AGENT.md', target: 'kimi:0.0', path: '/Users/marscrazy/workspace/new_eda',
  session: 'main', window: '1', window_name: '', index: '0',
};
const CLAUDE_6 = {
  id: '%4', number: 6, command: 'claude', provider: 'claude',
  title: 'Review | repo', target: 'codex:0.0', path: '/work/repo',
  session: 'codex', window: '0', window_name: 'editor', index: '0',
};
const SHELL = {
  id: '%9', number: null, command: 'zsh', provider: '',
  title: 'zsh', target: 'main:0.0', path: '/home/congyong',
  session: 'main', window: '2', window_name: '', index: '9',
};

const HOSTS = [
  { ...MACMINI, panes: [HERMES_39, KIMI_42] },
  { ...LOCAL, panes: [CLAUDE_6, SHELL] },
];

const MODEL_PROMPT = {
  kind: 'menu', title: 'Select a model', hint: '↑↓ navigate · Enter select',
  options: [
    { label: 'Kimi K2 5' },
    { label: 'Gemini 3', description: 'fast model' },
  ],
  toggles: [],
};
const NEEDS = new Map([[HERMES_39.id, MODEL_PROMPT]]);

const terms = (query) => parsePaneSearchTerms(query);
const matches = (pane, host, query, needs) =>
  paneMatchesSearch(pane, host, terms(query), needs ? needs.get(pane.id) : undefined);

test('terms are split on whitespace, lowercased, and empty input gives no terms', () => {
  assert.deepEqual(terms('  HermES  #39 '), ['hermes', '#39']);
  assert.deepEqual(terms('a  b'), ['a', 'b']);
  assert.deepEqual(terms(''), []);
  assert.deepEqual(terms('   '), []);
  assert.deepEqual(terms(null), []);
});

test('a pane number matches with or without #', () => {
  assert.equal(matches(HERMES_39, MACMINI, '39'), true);
  assert.equal(matches(HERMES_39, MACMINI, '#39'), true);
  assert.equal(matches(HERMES_39, MACMINI, '40'), false);
  assert.equal(matches(HERMES_39, MACMINI, '139'), false);
  assert.equal(matches(CLAUDE_6, LOCAL, '6'), true);
  assert.equal(matches(CLAUDE_6, LOCAL, '#6'), true);
  // a pane without a number never matches a number term
  // ('9' alone would hit via the pane id '%9', so use a term that is nowhere)
  assert.equal(matches(SHELL, LOCAL, '99'), false);
});

test('the server name and id prefix match', () => {
  assert.equal(matches(HERMES_39, MACMINI, 'macmini'), true);
  assert.equal(matches(KIMI_42, MACMINI, 'macmini'), true);
  assert.equal(matches(CLAUDE_6, LOCAL, 'macmini'), false);
  assert.equal(matches(HERMES_39, MACMINI, '5ae77f56'), true);
  assert.equal(matches(CLAUDE_6, LOCAL, 'localhost'), true);
  assert.equal(matches(HERMES_39, MACMINI, 'MACMINI'), true);
});

test('the harness matches on command and provider', () => {
  assert.equal(matches(HERMES_39, MACMINI, 'hermes'), true);
  assert.equal(matches(HERMES_39, MACMINI, 'HERMES'), true);
  assert.equal(matches(KIMI_42, MACMINI, 'kimi'), true);
  assert.equal(matches(HERMES_39, MACMINI, 'kimi'), false);
  assert.equal(matches(CLAUDE_6, LOCAL, 'claude'), true);
  assert.equal(matches(SHELL, LOCAL, 'zsh'), true);
  assert.equal(matches(SHELL, LOCAL, 'claude'), false);
});

test('title, path, session and window names match', () => {
  assert.equal(matches(CLAUDE_6, LOCAL, 'review'), true);
  assert.equal(matches(CLAUDE_6, LOCAL, 'REVIEW'), true);
  assert.equal(matches(CLAUDE_6, LOCAL, 'repo'), true);
  assert.equal(matches(KIMI_42, MACMINI, 'agent.md'), true);
  assert.equal(matches(KIMI_42, MACMINI, 'new_eda'), true);
  assert.equal(matches(CLAUDE_6, LOCAL, 'codex'), true);
  assert.equal(matches(CLAUDE_6, LOCAL, 'editor'), true);
  assert.equal(matches(CLAUDE_6, LOCAL, 'hermes'), false);
});

test('a needs-you prompt matches on its title and option text', () => {
  assert.equal(matches(HERMES_39, MACMINI, 'model', NEEDS), true);
  assert.equal(matches(HERMES_39, MACMINI, 'k2', NEEDS), true);
  assert.equal(matches(HERMES_39, MACMINI, 'gemini', NEEDS), true);
  assert.equal(matches(HERMES_39, MACMINI, 'fast model', NEEDS), true);
  // the same words do not match a pane with no prompt
  assert.equal(matches(KIMI_42, MACMINI, 'gemini', NEEDS), false);
  assert.equal(matches(CLAUDE_6, LOCAL, 'k2', NEEDS), false);
});

test('multiple terms are AND: every term must hit some field', () => {
  assert.equal(matches(HERMES_39, MACMINI, 'macmini hermes'), true);
  assert.equal(matches(HERMES_39, MACMINI, '39 hermes'), true);
  assert.equal(matches(HERMES_39, MACMINI, 'macmini kimi'), false);
  assert.equal(matches(HERMES_39, MACMINI, '39 kimi'), false);
  assert.equal(matches(KIMI_42, MACMINI, 'macmini kimi'), true);
  assert.equal(matches(CLAUDE_6, LOCAL, 'review repo'), true);
  assert.equal(matches(CLAUDE_6, LOCAL, 'review hermes'), false);
});

test('an empty query matches every pane', () => {
  for (const pane of [HERMES_39, KIMI_42, CLAUDE_6, SHELL]) {
    assert.equal(paneMatchesSearch(pane, LOCAL, [], undefined), true);
  }
});

test('filterHostsForSearch keeps only matching panes and counts them', () => {
  const byHarness = filterHostsForSearch(HOSTS, terms('hermes'), NEEDS);
  assert.equal(byHarness.matched, 1);
  assert.deepEqual(byHarness.hosts.map((host) => host.id), ['5ae77f56bbb13200']);
  assert.deepEqual(byHarness.hosts[0].panes.map((pane) => pane.id), [HERMES_39.id]);

  const byHost = filterHostsForSearch(HOSTS, terms('macmini'), NEEDS);
  assert.equal(byHost.matched, 2);
  assert.deepEqual(byHost.hosts[0].panes.map((pane) => pane.id), [HERMES_39.id, KIMI_42.id]);

  const byTitle = filterHostsForSearch(HOSTS, terms('review'), NEEDS);
  assert.equal(byTitle.matched, 1);
  assert.deepEqual(byTitle.hosts.map((host) => host.id), ['localhost']);
  assert.deepEqual(byTitle.hosts[0].panes.map((pane) => pane.id), ['%4']);

  const none = filterHostsForSearch(HOSTS, terms('zzz'), NEEDS);
  assert.equal(none.matched, 0);
  assert.deepEqual(none.hosts, []);
});

test('filterHostsForSearch with no terms returns the input untouched', () => {
  const view = filterHostsForSearch(HOSTS, [], NEEDS);
  assert.equal(view.matched, 0);
  assert.equal(view.hosts, HOSTS, 'no copy: renderList behaves exactly as before');
  assert.equal(HOSTS[0].panes.length, 2, 'the input is not mutated');
  assert.equal(HOSTS[1].panes.length, 2);
});

test('a host whose panes all miss is dropped, not left empty', () => {
  // without the needs-you prompt in play, only the kimi pane matches
  const view = filterHostsForSearch(HOSTS, terms('kimi'));
  assert.deepEqual(view.hosts.map((host) => host.id), ['5ae77f56bbb13200']);
  assert.equal(view.matched, 1);
});

test('with a prompt, its option text counts as a match field', () => {
  // the hermes pane's model menu lists 'Kimi K2 5', so it matches 'kimi' too
  const view = filterHostsForSearch(HOSTS, terms('kimi'), NEEDS);
  assert.equal(view.matched, 2);
  assert.deepEqual(view.hosts[0].panes.map((pane) => pane.id), [HERMES_39.id, KIMI_42.id]);
});
