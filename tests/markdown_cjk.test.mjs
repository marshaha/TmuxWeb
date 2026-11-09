/* 中文加粗的 CommonMark 围栏问题：**任务：**它 默认不渲染。
 *
 * 这里直接拿仓库里那份 marked（页面加载的同一份）验证修正前后的真实输出。
 */
import {test} from 'node:test';
import assert from 'node:assert';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';
import {marked} from '../web/vendor/marked/marked.esm.js';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'web', 'assets', 'markdown-cjk.js'), 'utf8');

function load() {
  const context = {window: {}};
  vm.createContext(context);
  vm.runInContext(source, context);
  return context.window.TmuxMarkdownCjk;
}

const render = (helper, text) => helper.strip(
  marked.parse(helper.fix(text), {gfm: true, breaks: false, async: false}));

test('句尾中文标点后直接接正文的加粗现在会渲染', () => {
  const helper = load();
  const out = render(helper, '**任务：**它停了。');
  assert.match(out, /<strong>任务：<\/strong>它停了/);
});

test('列表项里的同款写法也修好', () => {
  const helper = load();
  const out = render(helper, '- **粗体：**后面还有字');
  assert.match(out, /<li><strong>粗体：<\/strong>后面还有字/);
});

test('本来就能渲染的写法不受影响', () => {
  const helper = load();
  for (const [text, pattern] of [
    ['**WebUI 什么时候能选 curvefix：**', /<strong>WebUI 什么时候能选 curvefix：<\/strong>/],
    ['他说**重点是**这个', /<strong>重点是<\/strong>/],
    ['**任务：** 它停了。', /<strong>任务：<\/strong> 它停了/],
    ['**A**B', /<strong>A<\/strong>B/],
  ]) {
    assert.match(render(helper, text), pattern, text);
  }
});

test('不留下零宽空格：渲染结果和复制出来的文字都干净', () => {
  const helper = load();
  const out = render(helper, '**任务：**它停了。');
  assert.equal(out.includes('\u200B'), false);
});

test('代码块里的内容一个字都不变', () => {
  const helper = load();
  const out = render(helper, '```\n**任务：**它停了。\n```');
  assert.match(out, /<code>\*\*任务：\*\*它停了。/);
});

test('普通文本（没有加粗）原样通过', () => {
  const helper = load();
  const text = '就是一句普通的中文，带个冒号：结束。';
  assert.match(render(helper, text), new RegExp(text.slice(0, 6)));
  assert.equal(render(helper, text).includes('<strong>'), false);
});
