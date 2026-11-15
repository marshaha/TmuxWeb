/* 代码块里的路径：整块是一条路径才当链接，命令行不算。 */
import {test} from 'node:test';
import assert from 'node:assert';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'web', 'assets', 'path-links.js'), 'utf8');

function load() {
  const window = {};
  // 只取 bareCodePath：不碰 DOM
  vm.runInNewContext(source, {window, document: {}, NodeFilter: {}, setTimeout, clearTimeout});
  return window.TmuxPaths;
}

test('代码块里只有一条绝对路径 → 认成路径', () => {
  const paths = load();
  assert.equal(paths.bareCodePath('/Users/marscrazy/Downloads/lamp_bird.mp4'),
               '/Users/marscrazy/Downloads/lamp_bird.mp4');
});

test('带换行和空格的命令不算路径', () => {
  const paths = load();
  assert.equal(paths.bareCodePath('ls /Users/marscrazy/Downloads'), '');
  assert.equal(paths.bareCodePath('/tmp/a\n/tmp/b'), '');
});

test('相对路径（带扩展名）也算', () => {
  const paths = load();
  assert.equal(paths.bareCodePath('results/out.md'), 'results/out.md');
});

test('接口/静态前缀不认，避免把 API 当文件', () => {
  const paths = load();
  assert.equal(paths.bareCodePath('/api/panes/%254/files/download'), '');
});

test('空块和超长块不认', () => {
  const paths = load();
  assert.equal(paths.bareCodePath(''), '');
  assert.equal(paths.bareCodePath('/' + 'a'.repeat(600)), '');
});
