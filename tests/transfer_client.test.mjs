/* 直连传输的前端胶水：探测、改写下发地址、拿不到就回落。
 *
 *   node --test tests/
 *
 * transfer.js 是给浏览器写的老式脚本（挂到 window 上），所以这里用 vm 把它
 * 加载进一个假窗口：localStorage、location、fetch 全部替换成可控替身。
 */
import {test} from 'node:test';
import assert from 'node:assert';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'web', 'assets', 'transfer.js'), 'utf8');

function load(options = {}) {
  const store = new Map();
  const calls = [];
  const window = {location: {href: 'https://chat.example.com/?pane=%2528'}};
  const context = {
    window,
    location: window.location,
    localStorage: {
      getItem: key => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, value),
      removeItem: key => store.delete(key),
    },
    setTimeout,
    clearTimeout,
    AbortController,
    URL,
    JSON,
    console,
    fetch: async (url, init = {}) => {
      calls.push({url, init});
      return options.fetch(url, init);
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return {transfer: context.window.TmuxTransfer, calls, store};
}

const json = body => ({ok: true, status: 200, json: async () => body});

test('探测通过后记住源站，并把下载链接改写到它上面', async () => {
  const {transfer, calls, store} = load({
    fetch: async url => {
      if (url.endsWith('/api/system/addresses')) {
        return json({addresses: [
          {url: 'http://10.0.0.5:8787', kind: 'lan', transfer: false},
          {url: 'https://robo.example.ts.net', kind: 'tailnet', transfer: true},
        ]});
      }
      if (url.endsWith('/t/ping')) return json({tmuxweb: true});
      if (url.endsWith('/api/transfer/ticket')) return json({token: 'abc.def', expires: 1});
      throw new Error('unexpected ' + url);
    },
  });
  const href = await transfer.downloadHref('/api/panes/%2524/files/download?path=/srv/a.bin');
  assert.equal(href, 'https://robo.example.ts.net/t/download?t=abc.def');
  assert.ok(store.get('tmuxweb.transfer.origin'), '源站应被记住');
  const ticketCall = calls.find(call => call.url.endsWith('/api/transfer/ticket'));
  assert.equal(JSON.parse(ticketCall.init.body).direction, 'down');
});

test('记得的源站第二次不再探测', async () => {
  const {transfer, calls, store} = load({
    fetch: async url => {
      if (url.endsWith('/t/ping')) return json({tmuxweb: true});
      if (url.endsWith('/api/transfer/ticket')) return json({token: 'x.y'});
      if (url.endsWith('/api/system/addresses')) return json({addresses: [{url: 'https://a.b.c', transfer: true}]});
      throw new Error('unexpected ' + url);
    },
  });
  store.set('tmuxweb.transfer.origin', JSON.stringify({origin: 'https://cached.example', at: Date.now()}));
  const href = await transfer.downloadHref('/api/panes/%2524/files/download?path=/srv/a.bin');
  assert.equal(href, 'https://cached.example/t/download?t=x.y');
  assert.equal(calls.filter(call => call.url.endsWith('/t/ping')).length, 0);
});

test('探测不通就原样返回，下载不受影响', async () => {
  const {transfer} = load({
    fetch: async url => {
      if (url.endsWith('/api/system/addresses')) return json({addresses: [{url: 'https://robo.example.ts.net', transfer: true}]});
      throw new TypeError('Failed to fetch');
    },
  });
  const original = '/api/panes/%2524/files/download?path=/srv/a.bin';
  assert.equal(await transfer.downloadHref(original), original);
});

test('拿不到票据也回落，绝不抛给调用方', async () => {
  const {transfer} = load({
    fetch: async url => {
      if (url.endsWith('/t/ping')) return json({tmuxweb: true});
      if (url.endsWith('/api/system/addresses')) return json({addresses: [{url: 'https://robo.example.ts.net', transfer: true}]});
      return {ok: false, status: 403, json: async () => ({detail: 'nope'})};
    },
  });
  const original = '/api/panes/%2524/files/download?path=/srv/a.bin';
  assert.equal(await transfer.downloadHref(original), original);
});

test('不是下载接口的链接一律不动', async () => {
  const {transfer, calls} = load({fetch: async () => json({addresses: []})});
  for (const href of ['https://example.com/x.zip', '/share/abc', '/api/panes/%2524/files/zip']) {
    assert.equal(await transfer.downloadHref(href), href);
  }
  assert.equal(calls.length, 0, '不该为此发任何请求');
});

test('上传：探测通过后给 uploads.js 一个带票据的前缀', async () => {
  const {transfer} = load({
    fetch: async url => {
      if (url.endsWith('/api/system/addresses')) return json({addresses: [{url: 'https://robo.example.ts.net', transfer: true}]});
      if (url.endsWith('/t/ping')) return json({tmuxweb: true});
      if (url.endsWith('/api/transfer/ticket')) return json({token: 'up.tok'});
      throw new Error('unexpected ' + url);
    },
  });
  const route = await transfer.uploadRoute({pane: '%2524', dir: '/tmp', name: 'x.bin', size: 4096});
  assert.deepEqual(route, {prefix: 'https://robo.example.ts.net', ticket: 'up.tok'});
});

test('forget() 清掉记住的源站', async () => {
  const {transfer, store} = load({fetch: async () => json({addresses: []})});
  store.set('tmuxweb.transfer.origin', JSON.stringify({origin: 'https://gone.example', at: Date.now()}));
  transfer.forget();
  assert.equal(store.has('tmuxweb.transfer.origin'), false);
});
