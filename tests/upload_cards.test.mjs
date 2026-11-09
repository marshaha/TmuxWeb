/* 附件预览：`user uploaded file “a.png”` 该变成什么。
 *
 * 这些函数只拼字符串，不需要 DOM —— 用 vm 跑一遍脚本、拿到 window.TmuxUploads 就够了。
 */
import {test} from 'node:test';
import assert from 'node:assert';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'web', 'assets', 'upload-cards.js'), 'utf8');

function load() {
  const window = {};
  vm.runInNewContext(source, {window, encodeURIComponent, String});
  return window.TmuxUploads;
}

test('一张图片变成缩略图，并指向 pane 的 uploads 地址', () => {
  const cards = load();
  const html = cards.bubbleHtml('user uploaded file “pasted-20261109-182641.png”', 'local~%28');
  assert.match(html, /class="upload-grid"/);
  assert.match(html, /<img[^>]*src="\/api\/panes\/local~%2528\/uploads\/pasted-20261109-182641\.png"/);
  assert.match(html, /data-upload="1"/, '标记出来，免得被"本地文件"改写规则当成路径');
  assert.doesNotMatch(html, /user uploaded file/);
});

test('多个文件名用 & 分隔，每个一张', () => {
  const cards = load();
  const html = cards.bubbleHtml('user uploaded file “a.png & b.jpg & notes.md”', '%1');
  assert.equal((html.match(/class="upload-thumb"/g) || []).length, 2, '两张图两个缩略图');
  assert.equal((html.match(/class="upload-file"/g) || []).length, 1, '非图片给一个文件片');
  assert.match(html, /notes\.md/);
});

test('文件名里的中文和空格要编码进 URL，但显示保持原样', () => {
  const cards = load();
  const html = cards.bubbleHtml('user uploaded file “每日 报表.xlsx”', '%7');
  assert.match(html, /uploads\/%E6%AF%8F%E6%97%A5%20%E6%8A%A5%E8%A1%A8\.xlsx/);
  assert.match(html, /每日 报表\.xlsx/);
});

test('文件名里单个 & 不算分隔符', () => {
  const cards = load();
  const html = cards.bubbleHtml('user uploaded file “a&b.png”', '%1');
  assert.equal((html.match(/class="upload-thumb"/g) || []).length, 1, '还是一个文件');
  assert.match(html, /uploads\/a%26b\.png/);
});

test('附件行前后的正文都保留，并且照旧转义', () => {
  const cards = load();
  const html = cards.bubbleHtml('看这张 <b>图</b>\nuser uploaded file “x.png”\n然后呢', '%3');
  assert.match(html, /看这张 &lt;b&gt;图&lt;\/b&gt;/);
  assert.match(html, /然后呢/);
  assert.doesNotMatch(html, /user uploaded file/);
});

test('没有附件行的普通消息原样转义', () => {
  const cards = load();
  assert.equal(cards.bubbleHtml('<script>x</script>', '%1'), '&lt;script&gt;x&lt;/script&gt;');
});

test('没有 pane 就退回纯文本，别把消息吃掉', () => {
  const cards = load();
  const text = 'user uploaded file “a.png”';
  assert.equal(cards.bubbleHtml(text, ''), 'user uploaded file “a.png”');
});

test('扩展名判断只认图片', () => {
  const cards = load();
  for (const name of ['a.png', 'b.JPG', 'c.jpeg', 'd.webp', 'e.gif', 'f.svg']) {
    assert.ok(cards.isImage(name), name);
  }
  for (const name of ['a.md', 'b.zip', 'c.png.txt', 'd']) {
    assert.ok(!cards.isImage(name), name);
  }
});
