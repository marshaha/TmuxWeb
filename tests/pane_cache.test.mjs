import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = path.resolve(import.meta.dirname, '..');

function load() {
  const store = new Map();
  const localStorage = {
    get length() { return store.size; },
    key: index => [...store.keys()][index] ?? null,
    getItem: key => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: key => store.delete(key),
  };
  const context = {window: {}, localStorage, JSON, Date, console};
  context.window = context;
  vm.createContext(context);
  const code = fs.readFileSync(path.join(ROOT, 'web/assets/pane-cache.js'), 'utf8');
  vm.runInContext(code, context);
  return {cache: context.window.TmuxPaneCache, store};
}

const host = (id, panes = 2) => ({
  id, name: id, kind: 'ssh',
  panes: Array.from({length: panes}, (_, i) => ({
    id: `${id}~%${i}`, number: i, session: 'work', window: 1, window_name: 'api',
    path: '/srv', title: '', heavy: 'x'.repeat(50),
  })),
});

test('hosts 只存画树需要的字段', () => {
  const {cache} = load();
  assert.equal(cache.saveHosts([host('h1')]), true);
  const read = cache.readHosts();
  assert.equal(read.hosts.length, 1);
  assert.equal(read.hosts[0].panes.length, 2);
  assert.equal(read.hosts[0].panes[0].heavy, undefined);
});

test('过期的 hosts 缓存不返回', () => {
  const {cache} = load();
  cache.saveHosts([host('h1')]);
  assert.equal(cache.readHosts(-1), null);
});

test('transcript 只留最后 40 条', () => {
  const {cache} = load();
  const messages = Array.from({length: 120}, (_, i) => ({id: String(i), role: 'user', text: 'x' + i}));
  cache.saveTranscript('%1', {messages, mtime: 1, provider: 'hermes'});
  const read = cache.readTranscript('%1');
  assert.equal(read.messages.length, 40);
  assert.equal(read.messages[39].id, '119');
});

test('条数超出上限时先丢最久没看的', () => {
  const {cache, store} = load();
  for (let i = 0; i < 20; i += 1) {
    cache.saveTranscript(`%${i}`, {messages: [{id: '1', text: 'x'}], mtime: i});
    // 让 at 递增，避免同一毫秒内排序不稳
    const key = `tmuxweb.cache.tx.%${i}`;
    const value = JSON.parse(store.get(key));
    value.at = 1000 + i;
    store.set(key, JSON.stringify(value));
  }
  const keys = [...store.keys()].filter(k => k.startsWith('tmuxweb.cache.tx.'));
  assert.ok(keys.length <= 12, `应保留不超过 12 条，实际 ${keys.length}`);
  assert.equal(cache.readTranscript('%19') !== null, true, '刚看的那条不能删');
  assert.equal(cache.readTranscript('%0'), null, '最久没看的先删');
});

test('clear 清掉全部', () => {
  const {cache, store} = load();
  cache.saveHosts([host('h1')]);
  cache.saveTranscript('%1', {messages: [{id: '1', text: 'x'}]});
  cache.clear();
  assert.equal([...store.keys()].length, 0);
});
