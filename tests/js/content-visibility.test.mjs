// content-visibility 虚拟化已经拿掉了，这个文件从"验证它有"变成"验证它别回来"。
//
// 背景：当年照 Open WebUI 的做法给离屏消息挂 content-visibility:auto 省绘制，
// 结果 WebKit 上留下空白消息（#26712），Windows Chrome 更狠 —— 侧边栏点开 #378
// 直接 STATUS_BREAKPOINT 整页崩（2026-11-20，?nocv=1 关掉就正常）。
// 转录最多 300 条、每条几 KB，省下来的绘制时间不值这个风险。
//
// 运行：node --test tests/js/content-visibility.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const page = fs.readFileSync(path.join(root, '..', '..', 'web', 'assets', 'console-app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, '..', '..', 'web', 'assets', 'console-base.css'), 'utf8');

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

const src = [grab('virtualizedClass'), grab('esc'), grab('msgResendButton'), grab('transcriptBubble')].join('\n');
const virtualizedClass = new Function(src + '\nreturn virtualizedClass;')();
const bubble = new Function(
  'markdown', 'userBubbleHtml',
  src + '\nreturn transcriptBubble;',
)((text) => `<p>${text}</p>`, (message) => message.text);

test('没有任何一条消息再被打上 content-visibility', () => {
  for (const [index, total] of [[0, 5], [3, 5], [4, 5], [0, 1]]) {
    assert.equal(virtualizedClass(index, total), '',
      'virtualizedClass 必须永远返回空串：返回东西就说明这条 CSS 又接回来了');
  }
});

test('CSS 里不再有跳过绘制的那条规则', () => {
  assert.ok(!css.includes('.msg-virtualized{content-visibility'), 'content-visibility 规则不许回来');
  assert.ok(!css.includes('contain-intrinsic-size'), 'contain-intrinsic-size 跟着一起不许回来');
});

test('transcriptBubble 不再往气泡上盖 msg-virtualized', () => {
  const user = { id: 'u1', role: 'user', text: 'hi' };
  const asst = { id: 'a1', role: 'assistant', text: 'hello' };
  for (const node of [bubble(user, 0, 3), bubble(asst, 1, 3), bubble(asst, 2, 3)]) {
    assert.doesNotMatch(node, /msg-virtualized/);
  }
});

test('两条渲染路径仍然把 index/total 传进来（换实现时不用再动调用点）', () => {
  assert.ok(page.includes('virtualizedClass(index,messages.length)'), '正式列表要传 index');
  assert.ok(page.includes('transcriptBubble(message,rendered.length+offset,messages.length)'), '追加路径要传 index');
});

test('流式更新仍然原地替换，不整段重建', () => {
  // 整段 innerHTML 重建会让滚到一半的读者跳位；同 id + 只改文本时只替换那一条。
  assert.ok(page.includes('const prevText=state.messageText||new Map();'), '要留旧文本做 diff');
  assert.ok(page.includes('if(!pending.length&&rendered.length===ids.length'), '原地更新要求 id 一致且没有 pending');
  assert.ok(page.includes('node.replaceWith(...wrap.childNodes)'), '只替换变化的那条');
  assert.ok(page.includes('enhancePathsLazy(host,true)'), '原地更新后同样要重新做路径处理');
});

test('加载更早消息按"可见消息"锚定，而不是按 scrollHeight 差', () => {
  assert.ok(page.includes('function firstVisibleMessage()'), '锚点辅助函数要在');
  assert.ok(page.includes('function restoreAnchor(anchor)'), '恢复辅助函数要在');
  assert.ok(page.includes('const previous=state.transcriptMessages.length,anchor=firstVisibleMessage();'),
    'fetch 之前要取锚点');
  assert.ok(page.includes('requestAnimationFrame(()=>restoreAnchor(anchor));'),
    '下一帧再校正一次，等邻居测量完');
  assert.ok(!page.includes('scroll.scrollTop+=scroll.scrollHeight-height'),
    'scrollHeight 差值补偿必须删掉');
});
