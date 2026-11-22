import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.dirname(new URL(import.meta.url).pathname);
const page = fs.readFileSync(path.join(root, '..', '..', 'web', 'assets', 'console-app.js'), 'utf8');

test('聊天全量显示加载的记录，并且至多保留最近 500 条', () => {
  assert.match(page, /TRANSCRIPT_PAGE_MAX=500/);
  assert.match(page, /messages=messages\.slice\(-TRANSCRIPT_PAGE_MAX\)/);
  assert.doesNotMatch(page, /WIN_OVERRIDE|growWindowUp|trimWindow/);
});

test('渲染保留同一消息节点，只更新已经变动的消息', () => {
  assert.match(page, /before=state\.renderedMessages/);
  assert.match(page, /revision\.every/);
  assert.match(page, /if\(next\.get\(id\)===item\)continue/);
  assert.match(page, /state\.renderedMessages=new Map/);
});

test('从缓存画出来后的第一次刷新走全量，补齐缓存里没有的更早消息', () => {
  assert.ok(page.includes('state.needsFullTranscript=true;'), '缓存渲染时打标记');
  assert.ok(page.includes('const fullFirst=!!state.needsFullTranscript;state.needsFullTranscript=false;'));
  assert.ok(page.includes('(!force&&!refresh&&!fullFirst&&state.transcriptMessages.length)'),
    '有标记就不要带 since，否则永远补不回缓存丢掉的头几条');
  assert.ok(page.includes('const follow=atBottom()||fullFirst'), '补齐之后要还在底部');
});
