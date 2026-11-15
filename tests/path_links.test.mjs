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

test('代码块里"路径 + 说明"混排时，路径那一行仍然认', () => {
  const paths = load();
  // #179 那条消息的形态：第一行是视频路径，下面几行是时长/分辨率
  assert.equal(paths.bareCodePath('/Users/marscrazy/Downloads/bathhouse_itching_powder.mp4'),
               '/Users/marscrazy/Downloads/bathhouse_itching_powder.mp4');
  // 说明那行不是路径
  assert.ok(!paths.bareCodePath('26.4 秒 · 960×544（16:9）· 24fps · 632 帧 · 5.7 MB'));
});

test('时间码不会被当成文件路径', () => {
  const paths = load();
  // 分镜里的 12.5/12.8s 曾经被当成相对路径，扩展名至少两个字才认
  assert.ok(!paths.bareCodePath('12.5/12.8s'));
});
