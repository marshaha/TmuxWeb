/* 代码块里的路径：整块是一条路径才当链接，命令行不算。 */
import {test} from 'node:test';
import assert from 'node:assert';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'web', 'assets', 'path-links.js'), 'utf8');

function load(documentStub) {
  const window = {};
  // 只取 bareCodePath：不碰 DOM
  vm.runInNewContext(source, {window, document: documentStub || {},
    NodeFilter: {SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2}, setTimeout, clearTimeout});
  return window.TmuxPaths;
}

// 走真实 enhance()：给它一个最小 fake DOM（一个文本节点 + 一个 div 父级），
// 返回识别出的链接路径和重组后的全文——全文必须和输入一字不差，链接只含路径本身。
function enhanceLinks(text) {
  class El {
    constructor(tag) {
      this.tagName = String(tag).toUpperCase();
      this.children = [];
      this.dataset = {};
    }
    addEventListener() {}
    appendChild(child) { this.children.push(child); return child; }
    set textContent(value) { this._text = value; }
    get textContent() {
      if (this._text !== undefined) return this._text;
      return this.children.map(child => child.textContent || '').join('');
    }
    closest() { return null; }
    querySelectorAll() { return []; }
  }
  class TextNode {
    constructor(value) { this.nodeValue = value; this.textContent = value; }
  }
  class Fragment {
    constructor() { this.children = []; }
    appendChild(child) { this.children.push(child); return child; }
  }
  const node = new TextNode(text);
  let replaced = null;
  node.parentNode = {replaceChild(fragment) { replaced = fragment; }};
  node.parentElement = {tagName: 'DIV', closest: () => null};
  const documentStub = {
    createElement: tag => new El(tag),
    createTextNode: value => new TextNode(value),
    createDocumentFragment: () => new Fragment(),
    createTreeWalker: (_root, _type, filter) => {
      let done = false;
      return {nextNode() {
        if (done) return null;
        done = true;
        return filter.acceptNode(node) === 1 ? node : null;
      }};
    },
    body: new El('body'),
  };
  const paths = load(documentStub);
  paths.enhance({querySelectorAll: () => []}, {pane: 'p1', cwd: '/home/x'});
  if (!replaced) return {links: [], text};
  const links = replaced.children
    .filter(child => child.dataset && child.dataset.plinkPath !== undefined)
    .map(child => child.dataset.plinkPath);
  return {links, text: replaced.children.map(child => child.textContent || '').join('')};
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

test('全角左括号不进路径：用户报的 authorized_keys（3', () => {
  // 中文注释“（3个）”里的（不是路径的一部分：以前整段都被链成一个链接
  const one = enhanceLinks('/Users/marscrazy/.ssh/authorized_keys（3');
  assert.deepEqual(one.links, ['/Users/marscrazy/.ssh/authorized_keys']);
  assert.equal(one.text, '/Users/marscrazy/.ssh/authorized_keys（3');
  const sentence = enhanceLinks('见 /Users/marscrazy/.ssh/authorized_keys（3个）这个文件');
  assert.deepEqual(sentence.links, ['/Users/marscrazy/.ssh/authorized_keys']);
  assert.equal(sentence.text, '见 /Users/marscrazy/.ssh/authorized_keys（3个）这个文件');
});

test('同类全角左开符号也不进路径：【 《', () => {
  assert.deepEqual(enhanceLinks('/tmp/a【x').links, ['/tmp/a']);
  assert.deepEqual(enhanceLinks('/tmp/a《x').links, ['/tmp/a']);
  assert.deepEqual(enhanceLinks('~/Downloads/x（2）.mp4').links, ['~/Downloads/x']);
  assert.deepEqual(enhanceLinks('./out（draft）.md').links, ['./out']);
});

test('全角右闭符号也不进路径：》 」 』（）】 本来就拦得住）', () => {
  // 》」』和）】是同一类不对称：BOUNDARY 里有，路径字符集里原来没有
  assert.deepEqual(enhanceLinks('/tmp/a》x').links, ['/tmp/a']);
  assert.deepEqual(enhanceLinks('/tmp/a」x').links, ['/tmp/a']);
  assert.deepEqual(enhanceLinks('/tmp/a』x').links, ['/tmp/a']);
  assert.deepEqual(enhanceLinks('/tmp/a）x').links, ['/tmp/a']);
  assert.deepEqual(enhanceLinks('/tmp/a】x').links, ['/tmp/a']);
});

test('正常路径不受影响，ASCII 左括号先不动', () => {
  assert.deepEqual(enhanceLinks('/Users/marscrazy/.ssh/authorized_keys').links,
    ['/Users/marscrazy/.ssh/authorized_keys']);
  assert.deepEqual(enhanceLinks('~/Downloads/lamp_bird.mp4').links, ['~/Downloads/lamp_bird.mp4']);
  assert.deepEqual(enhanceLinks('./results/out.md').links, ['./results/out.md']);
  // 空格本来就会断开路径：(1) 不影响前面的路径段
  assert.deepEqual(enhanceLinks('/a/b (1).txt').links, ['/a/b']);
  // ASCII ( 紧贴路径时是真实路径的一部分（/a/b(1).txt 确实存在），先不动；
  // 只是 ) 本来就在排除列表里，所以现在链到 /a/b(1 为止——这是原有行为，不是本次改动
  assert.deepEqual(enhanceLinks('/a/b(1).txt').links, ['/a/b(1']);
});
