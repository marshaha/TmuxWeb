// 转录窗口化：DOM 里只留最近的/附近的若干条，滚到顶往上加，回到底部再收回。
//
// 为什么不用 content-visibility：那条 CSS 会让 Windows Chrome 的渲染进程整页崩（#378）。
// 所以这里是"显式窗口"：切片渲染 + 用可见消息做锚点，不用占位高度估算。
// 真实滚动行为在浏览器里验过（winsize=10：初始 10 条 → 滚到顶 30 条且锚点不跳 → 回底部收回 10 条），
// 这里钉住的是"调用点没被改坏"。
//
// 运行：node --test tests/js/transcript-window.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const page = fs.readFileSync(path.join(root, '..', '..', 'web', 'assets', 'console-app.js'), 'utf8');

test('窗口大小有常量，并且能用 ?winsize=N 覆盖（自测用）', () => {
  assert.match(page, /const WIN_OVERRIDE=Number\(URL_FLAGS\.get\('winsize'\)\)\|\|0;/);
  assert.match(page, /const WIN_INITIAL=WIN_OVERRIDE\|\|80;/);
  assert.match(page, /const WIN_GROW=WIN_OVERRIDE\|\|80;/);
  assert.match(page, /const WIN_MAX=\(WIN_OVERRIDE\?WIN_OVERRIDE\*2:0\)\|\|240;/);
});

test('渲染只拼窗口里的那一段，并且有硬上限', () => {
  assert.ok(page.includes('const end=Math.min(total,start+WIN_MAX);'), '窗口要有上限，翻了很多历史也不会全渲染');
  assert.ok(page.includes('const shown=messages.slice(start,end);'), '切片渲染');
  assert.ok(page.includes('const ids=shown.map(message=>String(message.id));'), 'renderedIds 记的是窗口内的 id');
});

test('往上扩 = 先记锚点、加内容、按几何位置把锚点放回去', () => {
  assert.ok(page.includes('function growWindowUp()'), 'growWindowUp 要在');
  assert.ok(page.includes('const anchor=firstVisibleMessage();'), '扩之前要记住读者眼前那条');
  assert.ok(page.includes('state.windowStart=Math.max(0,start-WIN_GROW);'));
  assert.ok(page.includes('requestAnimationFrame(()=>{restoreAnchor(anchor);state.stick=false;'),
    '扩完必须按几何位置恢复，不然页面会往上窜');
});

test('回收 = 只在贴底时砍掉头部，不打断正在往上读的人', () => {
  assert.ok(page.includes('function trimWindow()'), 'trimWindow 要在');
  assert.ok(page.includes('if(total-start<=WIN_MAX)return false;'), '没超上限不动');
  assert.ok(page.includes('state.windowStart=Math.max(0,total-WIN_INITIAL);'), '回收到最近一屏');
});

test('滚动钩子：先扩本地窗口，扩到头才联网要更早的', () => {
  assert.ok(page.includes('if(scroll.scrollTop<160){if(!growWindowUp()&&scroll.scrollTop<32&&state.hasMore&&!state.loadingEarlier)loadEarlier();}'),
    '滚到顶的顺序必须是 先本地扩、再联网');
  assert.ok(page.includes('else if(state.stick)trimWindow();'), '在底部看最新消息时回收窗口');
});

test('切 pane 时窗口重置（否则新会话会继承上一个的偏移）', () => {
  assert.ok(page.includes('state.transcriptMessages=[];state.windowStart=null;'), '切会话要清窗口');
});

test('从缓存画出来后的第一次刷新走全量，补齐缓存里没有的更早消息', () => {
  assert.ok(page.includes('state.needsFullTranscript=true;'), '缓存渲染时打标记');
  assert.ok(page.includes('const fullFirst=!!state.needsFullTranscript;state.needsFullTranscript=false;'));
  assert.ok(page.includes('(!force&&!refresh&&!fullFirst&&state.transcriptMessages.length)'),
    '有标记就不要带 since，否则永远补不回缓存丢掉的头几条');
  assert.ok(page.includes('const follow=atBottom()||fullFirst'), '补齐之后要还在底部');
});
