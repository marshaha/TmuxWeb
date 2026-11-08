/* 视频卡片：什么链接该变成播放器、什么绝对不该。
 *
 * 没有 jsdom 依赖，这里用一个巴掌大的假 DOM：只实现 enhance() 真正用到的那几个
 * API（querySelectorAll / getAttribute / dataset / closest / replaceWith / append）。
 */
import {test} from 'node:test';
import assert from 'node:assert';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'web', 'assets', 'video-cards.js'), 'utf8');

function makeElement(tag) {
  return {
    tagName: tag.toUpperCase(),
    className: '',
    textContent: '',
    title: '',
    href: '',
    src: '',
    dataset: {},
    attributes: {},
    children: [],
    classList: {add() {}},
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name]; },
    append(...nodes) { this.children.push(...nodes); },
    appendChild(node) { this.children.push(node); return node; },
    closest() { return null; },
    replaceWith(node) { this.replacedWith = node; },
  };
}

function run(hrefs) {
  const created = [];
  const links = hrefs.map(href => {
    const link = makeElement('a');
    link.attributes.href = href;
    return link;
  });
  const context = {
    window: {},
    location: {origin: 'https://chat.example.com'},
    URL,
    document: {
      createElement(tag) { const el = makeElement(tag); created.push(el); return el; },
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  const made = context.window.TmuxVideo.enhance({querySelectorAll: () => links});
  return {made, links, created, api: context.window.TmuxVideo};
}

test('mp4 链接变成播放器，并且保留原链接', () => {
  const href = 'https://alist.example.com/d/189Cloud/videos/xiajiang_moonlit_rescue.mp4';
  const {made, links, created} = run([href]);
  assert.equal(made, 1);
  const figure = links[0].replacedWith;
  assert.equal(figure.className, 'video-card');
  const video = figure.children[0];
  assert.equal(video.tagName, 'VIDEO');
  assert.equal(video.src, href);
  assert.equal(video.preload, 'metadata', '必须只取元信息，不能自动下载整段');
  assert.equal(video.playsInline, true);
  assert.equal(video.controls, true);
  const caption = figure.children[1];
  assert.equal(caption.children[0].textContent, 'xiajiang_moonlit_rescue.mp4');
  assert.equal(caption.children[0].href, href);
  assert.ok(created.length >= 3);
});

test('本地文件路径（/files/raw?path=…mp4）也认', () => {
  const href = '/api/panes/%2524/files/raw?path=%2Ftmp%2Fdemo.mp4';
  const {made, links} = run([href]);
  assert.equal(made, 1);
  assert.equal(links[0].replacedWith.children[0].src, 'https://chat.example.com' + href);
});

test('视频以外的链接一律不动', () => {
  const hrefs = [
    'https://example.com/files/report.pdf',
    'https://example.com/page',
    'https://example.com/video.mp4.txt',
    '/share/abc',
  ];
  const {made, links} = run(hrefs);
  assert.equal(made, 0);
  links.forEach(link => assert.equal(link.replacedWith, undefined));
});

test('危险协议不嵌入', () => {
  for (const href of ['javascript:alert(1)//x.mp4', 'data:video/mp4;base64,AAA']) {
    const {made, links} = run([href]);
    assert.equal(made, 0, href);
    assert.equal(links[0].replacedWith, undefined);
  }
});

test('同一条消息渲染两次不会套两层卡片', () => {
  const el = makeElement('a');
  el.attributes.href = 'https://example.com/a.mp4';
  const context = {
    window: {}, location: {origin: 'https://chat.example.com'}, URL,
    document: {createElement: makeElement},
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  // 第一次：替换成卡片
  context.window.TmuxVideo.enhance({querySelectorAll: () => [el]});
  assert.ok(el.replacedWith);
  // 第二次：原链接已经带 videoDone，且它已在卡片里 → 不再处理
  el.closest = () => ({className: 'video-card'});
  assert.equal(context.window.TmuxVideo.enhance({querySelectorAll: () => [el]}), 0);
});

test('扩展名大小写和查询串都认', () => {
  const {api} = run([]);
  assert.ok(api.VIDEO_URL_RE.test('.MP4'));
  assert.ok(api.VIDEO_URL_RE.test('a.webm?token=1'));
  assert.ok(!api.VIDEO_URL_RE.test('a.mp4.png'));
});
