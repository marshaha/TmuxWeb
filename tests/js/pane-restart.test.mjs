// Unit tests for the per-pane restart-and-resume UI in web/index.html.
// Run with: node --test tests/js/pane-restart.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const indexPage = fs.readFileSync(path.join(root, '..', '..', 'web', 'index.html'), 'utf8');

function grab(page, name) {
  for (const prefix of ['  async function ' + name + '(', '  function ' + name + '(']) {
    const start = page.indexOf(prefix);
    if (start === -1) continue;
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
  throw new Error(`missing function ${name}`);
}

const restartProvider = new Function(
  grab(indexPage, 'restartProvider') + 'return restartProvider;')();
const restartSelectedPane = new Function(
  grab(indexPage, 'restartSelectedPane') + 'return restartSelectedPane;')();

test('the top bar has a restart button and a dialog that names the scrollback cost', () => {
  assert.match(indexPage, /id="pane-restart"[^>]*title="重启并恢复会话"/);
  assert.match(indexPage, /id="pane-restart"[^>]*><svg class="ico"><use href="#i-refresh"\/>/);
  assert.match(indexPage, /id="pane-restart-dialog"/);
  assert.match(indexPage, /滚动记录会清空，对话从会话文件重新显示/);
  assert.match(indexPage, /\$\('pane-restart'\)\.onclick=\(\)=>openPaneRestartDialog\(\);/);
  assert.match(indexPage, /\$\('pane-restart-confirm'\)\.onclick=\(\)=>restartSelectedPane\(\);/);
});

test('the restartable set matches the backend provider list', () => {
  const match = indexPage.match(/const RESTARTABLE_PROVIDERS=\[([^\]]+)\]/);
  assert.ok(match, 'missing RESTARTABLE_PROVIDERS');
  assert.deepEqual(JSON.parse('[' + match[1].replace(/'/g, '"') + ']'),
                   ['claude', 'codex', 'kimi', 'hermes']);
});

test('the button is enabled only for a restartable provider', () => {
  assert.match(indexPage, /RESTARTABLE_PROVIDERS\.includes\(restartProvider\(\)\)/);
});

test('restartProvider prefers the pane provider, falls back to the transcript provider', () => {
  globalThis.state = {
    selected: '%4',
    panes: [{ id: '%4', provider: 'codex' }],
    transcriptProvider: 'hermes',
  };
  assert.equal(restartProvider(), 'codex');
  globalThis.state.panes = [{ id: '%4' }];
  assert.equal(restartProvider(), 'hermes');
  globalThis.state = { selected: null, panes: [], transcriptProvider: '' };
  assert.equal(restartProvider(), '');
});

test('restartSelectedPane posts to restart-resume and refreshes on success', async () => {
  const statusEl = { textContent: '' };
  const dialogEl = { closed: false, close() { this.closed = true; } };
  const buttonEl = { disabled: false };
  const calls = [];
  globalThis.state = { selected: '%4', panes: [{ id: '%4', provider: 'codex' }], transcriptProvider: '' };
  globalThis.$ = (id) => ({ 'pane-restart-confirm': buttonEl,
                             'pane-restart-status': statusEl,
                             'pane-restart-dialog': dialogEl }[id]);
  globalThis.api = async (url, opts) => {
    calls.push([url, opts]);
    return { status: 'resumed', provider: 'codex', session_id: 'abc12345', verified: true };
  };
  globalThis.toast = (text) => calls.push(['toast', text]);
  globalThis.refreshTranscript = (id, opts) => calls.push(['refresh', id, opts]);
  globalThis.loadPanes = (force) => calls.push(['loadPanes', force]);

  await restartSelectedPane();
  assert.deepEqual(calls[0], ['/api/panes/%254/restart-resume', { method: 'POST' }]);
  assert.equal(dialogEl.closed, true);
  assert.equal(buttonEl.disabled, false);
  assert.ok(calls.some((c) => c[0] === 'toast' && /已重启并恢复 codex/.test(c[1])));
  assert.ok(calls.some((c) => c[0] === 'refresh' && c[1] === '%4'));
});

test('restartSelectedPane shows the backend refusal instead of closing the dialog', async () => {
  const statusEl = { textContent: '' };
  const dialogEl = { closed: false, close() { this.closed = true; } };
  const buttonEl = { disabled: false };
  globalThis.state = { selected: '%4', panes: [{ id: '%4', provider: 'codex' }], transcriptProvider: '' };
  globalThis.$ = (id) => ({ 'pane-restart-confirm': buttonEl,
                             'pane-restart-status': statusEl,
                             'pane-restart-dialog': dialogEl }[id]);
  globalThis.api = async () => { throw new Error('agent 还在忙，请稍后再试'); };
  globalThis.toast = () => { throw new Error('toast must not run on failure'); };
  globalThis.refreshTranscript = () => { throw new Error('no refresh on failure'); };
  globalThis.loadPanes = () => { throw new Error('no reload on failure'); };

  await restartSelectedPane();
  assert.equal(dialogEl.closed, false);
  assert.match(statusEl.textContent, /重启失败：agent 还在忙，请稍后再试/);
  assert.equal(buttonEl.disabled, false);
});
