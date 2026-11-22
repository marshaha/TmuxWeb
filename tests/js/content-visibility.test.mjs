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

test('CSS 里不再有跳过绘制的那条规则', () => {
  assert.ok(!css.includes('.msg-virtualized{content-visibility'), 'content-visibility 规则不许回来');
  assert.ok(!css.includes('contain-intrinsic-size'), 'contain-intrinsic-size 跟着一起不许回来');
});

test('聊天不会通过 CSS 类跳过离屏消息的绘制', () => {
  assert.doesNotMatch(page, /msg-virtualized|virtualizedClass/);
});

test('加载更早消息按"可见消息"锚定，而不是按 scrollHeight 差', () => {
  assert.ok(page.includes('function firstVisibleMessage()'), '锚点辅助函数要在');
  assert.ok(page.includes('function restoreAnchor(anchor)'), '恢复辅助函数要在');
  assert.ok(page.includes('const previous=state.transcriptMessages.length,anchor=firstVisibleMessage();'),
    'fetch 之前要取锚点');
  assert.ok(page.includes('requestAnimationFrame(()=>{restoreAnchor(anchor);'),
    '更新 DOM 后在下一帧恢复锚点');
  assert.ok(!page.includes('scroll.scrollTop+=scroll.scrollHeight-height'),
    'scrollHeight 差值补偿必须删掉');
});

test('展开历史有条数上限，到顶后按钮变禁用并说明原因', () => {
  // 没有虚拟化之后，DOM 就是实打实的节点：上限按节点预算算（约 24 节点/条，12k 封顶 → 500）。
  assert.match(page, /TRANSCRIPT_PAGE=300,TRANSCRIPT_PAGE_MAX=500/,
    '上限为 500 条');
  assert.ok(page.includes('const capped=state.transcriptLimit>=TRANSCRIPT_PAGE_MAX;'),
    '到顶要能被识别出来');
  assert.ok(page.includes('button.disabled=!!state.loadingEarlier||capped;'),
    '到顶后按钮禁用，不然点了没反应');
  assert.ok(page.includes('只保留最近 ${TRANSCRIPT_PAGE_MAX} 条'), '按钮要说清为什么点不动');
});
