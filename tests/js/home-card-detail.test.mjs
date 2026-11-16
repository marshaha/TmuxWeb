// Detail panel for board result/alert/note cards (web/assets/home-card-detail.js).
// Run with: node --test tests/js/home-card-detail.test.mjs
//
// The card body ships in full in the /api/home payload, but the card view
// clamps it to six lines (-webkit-line-clamp: 6) and these cards have no pane
// to preview, so the rest of the text was unreachable from the board. The
// panel must show the whole body, unclamped, without fetching anything.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.dirname(new URL(import.meta.url).pathname);
const asset = fs.readFileSync(
  path.join(root, '..', '..', 'web', 'assets', 'home-card-detail.js'), 'utf8');
const homeHtml = fs.readFileSync(
  path.join(root, '..', '..', 'web', 'home.html'), 'utf8');

// The reported card: its body was cut off in the card view exactly at
// "汇总：" — the file path, the SHA and "仅staging，未部署8770" were hidden.
const REPORTED = {
  id: 'ca7d3874c66f',
  title: '算法图库溯源与双视口验收完成',
  kind: 'result',
  created_at: 1794210831.7785094,
  pane_id: '',
  body: '任务A/B均完成。溯源27图/309面板/188完整图，265面板逐像素一致、0 mismatch；'
    + '24个H1缺失面板未伪造。8822主图库31/31、文档补验9/9，1440与真实390均通过。'
    + '#125已修复并排ROI偏移、candidate完整帧缺图和文档390撑宽。汇总：'
    + '`build/autonomy/round3-gallery-ui-126-final.md`（SHA `f6a8baf2…c763`）。'
    + '仅staging，未部署8770。',
};

function makeEl(id) {
  const classes = new Set();
  return {
    id,
    hidden: false,
    textContent: '',
    innerHTML: '',
    href: '',
    className: '',
    attrs: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
    },
    setAttribute: function (name, value) { this.attrs[name] = value; },
    getAttribute: function (name) { return this.attrs[name]; },
  };
}

function makeDoc() {
  const els = {};
  for (const id of ['preview-drawer', 'pd-frame', 'pd-backdrop', 'pd-title',
    'pd-console', 'pd-body', 'pd-kind', 'pd-time', 'pd-text', 'pd-foot',
    'pd-open-pane']) {
    els[id] = makeEl(id);
  }
  els['preview-drawer'].hidden = true;
  els['pd-body'].hidden = true;
  const bodyClasses = new Set();
  return {
    getElementById: (id) => els[id] || null,
    body: {
      classList: {
        add: (c) => bodyClasses.add(c),
        remove: (c) => bodyClasses.delete(c),
        contains: (c) => bodyClasses.has(c),
      },
    },
    _els: els,
    _bodyClasses: bodyClasses,
  };
}

function load() {
  const sandbox = {
    requestAnimationFrame: (fn) => fn(),
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(asset, sandbox, {filename: 'home-card-detail.js'});
  return sandbox.TmuxHomeCardDetail;
}

const detail = load();

function deps() {
  const seen = {mdCard: []};
  return {
    seen,
    mdCard: (value) => { seen.mdCard.push(value); return `<p>${value}</p>`; },
    consoleUrl: (pane) => `/?pane=${encodeURIComponent(pane)}`,
    formatTime: (ts) => `T${ts}`,
  };
}

test('findCard returns the card by id and null otherwise', () => {
  const cards = [REPORTED, {...REPORTED, id: 'other'}];
  assert.equal(detail.findCard(cards, 'ca7d3874c66f'), REPORTED);
  assert.equal(detail.findCard(cards, 'missing'), null);
  assert.equal(detail.findCard([], 'ca7d3874c66f'), null);
  assert.equal(detail.findCard(null, 'ca7d3874c66f'), null);
  assert.equal(detail.findCard(cards, ''), null);
});

test('openDetail shows the whole body, untruncated, with title/kind/time', () => {
  const doc = makeDoc();
  const d = deps();
  assert.equal(detail.openDetail(doc, REPORTED, d), true);
  // the renderer got the full body, not a slice of it
  assert.equal(d.seen.mdCard.length, 1);
  assert.equal(d.seen.mdCard[0], REPORTED.body);
  // the parts that were cut off behind the six-line clamp are all there
  const html = doc._els['pd-text'].innerHTML;
  assert.match(html, /汇总：/);
  assert.match(html, /round3-gallery-ui-126-final\.md/);
  assert.match(html, /f6a8baf2…c763/);
  assert.match(html, /仅staging，未部署8770/);
  assert.equal(doc._els['pd-title'].textContent, REPORTED.title);
  assert.equal(doc._els['pd-kind'].textContent, 'result');
  assert.equal(doc._els['pd-kind'].className, 'kind result');
  assert.equal(doc._els['pd-time'].textContent, `T${REPORTED.created_at}`);
});

test('openDetail swaps the iframe for the text body and opens the drawer', () => {
  const doc = makeDoc();
  assert.equal(detail.openDetail(doc, REPORTED, deps()), true);
  assert.equal(doc._els['preview-drawer'].hidden, false);
  assert.equal(doc._els['preview-drawer'].classList.contains('open'), true);
  assert.equal(doc._els['pd-backdrop'].hidden, false);
  assert.equal(doc._els['pd-frame'].hidden, true);
  assert.equal(doc._els['pd-body'].hidden, false);
  assert.equal(doc._els['preview-drawer'].getAttribute('aria-label'), '卡片详情');
  assert.equal(doc._bodyClasses.has('drawer-open'), true);
});

test('openDetail with a pane_id keeps the link into the pane', () => {
  const doc = makeDoc();
  const withPane = {...REPORTED, pane_id: '1d7ef54375eaabc3~%158'};
  assert.equal(detail.openDetail(doc, withPane, deps()), true);
  assert.equal(doc._els['pd-foot'].hidden, false);
  assert.equal(doc._els['pd-open-pane'].href, '/?pane=1d7ef54375eaabc3~%25158');
  assert.equal(doc._els['pd-console'].hidden, false);
  assert.equal(doc._els['pd-console'].href, '/?pane=1d7ef54375eaabc3~%25158');
});

test('openDetail without a pane_id hides both pane links', () => {
  const doc = makeDoc();
  assert.equal(detail.openDetail(doc, REPORTED, deps()), true);
  assert.equal(doc._els['pd-foot'].hidden, true);
  assert.equal(doc._els['pd-console'].hidden, true);
});

test('openDetail refuses a missing card or a missing drawer', () => {
  const doc = makeDoc();
  assert.equal(detail.openDetail(doc, null, deps()), false);
  assert.equal(doc._els['preview-drawer'].hidden, true);
  const noDrawer = makeDoc();
  delete noDrawer._els['preview-drawer'];
  assert.equal(detail.openDetail(noDrawer, REPORTED, deps()), false);
});

test('resetDetail hands the drawer back clean, and never blanks a reopen', () => {
  const doc = makeDoc();
  detail.openDetail(doc, REPORTED, deps());
  // simulate the close: the drawer slid out (.open removed) but is not
  // hidden yet — that is the state closeFullPreview's timer sees
  doc._els['preview-drawer'].classList.remove('open');
  detail.resetDetail(doc);
  assert.equal(doc._els['pd-body'].hidden, true);
  assert.equal(doc._els['pd-text'].innerHTML, '');
  assert.equal(doc._els['pd-frame'].hidden, false);
  assert.equal(doc._els['preview-drawer'].getAttribute('aria-label'), '对话完整预览');
  // a close followed by a quick reopen: the pending reset must not blank it
  detail.openDetail(doc, REPORTED, deps());
  detail.resetDetail(doc);
  assert.equal(doc._els['pd-body'].hidden, false);
  assert.match(doc._els['pd-text'].innerHTML, /仅staging，未部署8770/);
});

// --- wiring in home.html: the card carries its id, the click handler routes
// it to the panel before the pane-preview branch, and the delete button and
// body links keep their earlier branches. ---

test('home.html: board cards carry data-card and the click routes them first', () => {
  assert.match(homeHtml, /data-card="\$\{esc\(card\.id\)\}"/);
  assert.match(homeHtml, /<script src="\/assets\/home-card-detail\.js"><\/script>/);
  const del = homeHtml.indexOf("closest('[data-del]')");
  const bodyLink = homeHtml.indexOf("closest('.card-body a')");
  const card = homeHtml.indexOf("closest('[data-card]')");
  const open = homeHtml.indexOf("closest('[data-open]')");
  assert.ok(del !== -1 && bodyLink !== -1 && card !== -1 && open !== -1);
  assert.ok(del < card, 'delete still wins over the detail panel');
  assert.ok(bodyLink < card, 'body links still go to the browser, not the panel');
  assert.ok(card < open, 'board cards open the detail panel, not the pane preview');
  assert.match(homeHtml,
    /findCard\(state\.board&&state\.board\.cards\|\|\[\],detail\.dataset\.card\)/);
  assert.match(homeHtml,
    /openDetail\(document,card,\{\s*mdCard:value=>window\.mdCard\(value\),consoleUrl,formatTime:ago\}\)/);
});

test('home.html: the shared close hands the drawer back, the pane open restores it', () => {
  assert.match(homeHtml, /resetDetail\(document\)/);
  const open = homeHtml.indexOf('function openFullPreview');
  const close = homeHtml.indexOf('function closeFullPreview');
  assert.ok(open !== -1 && close !== -1);
  assert.match(homeHtml.slice(open, close), /\$\('pd-body'\)\.hidden=true;/);
  assert.match(homeHtml.slice(open, close), /drawerFrame\.hidden=false;/);
  assert.match(homeHtml.slice(open, close), /\$\('pd-console'\)\.hidden=false;/);
  assert.match(homeHtml.slice(close), /if\(drawer\.classList\.contains\('open'\)\)return;/);
});

test('home.html: the panel body is never clamped, and narrows to a bottom sheet', () => {
  assert.match(homeHtml, /id="pd-body"/);
  assert.match(homeHtml, /id="pd-text"/);
  assert.match(homeHtml, /id="pd-foot"/);
  assert.match(homeHtml, /id="pd-open-pane"/);
  assert.match(homeHtml, /\.pd-body\[hidden\]\{display:none\}/);
  assert.match(homeHtml, /#pd-console\[hidden\]\{display:none\}/);
  assert.match(homeHtml,
    /\.card-body\.pd-full\{display:block;overflow:visible;-webkit-line-clamp:unset\}/);
  const sheet = homeHtml.indexOf('@media(max-width:860px)');
  assert.ok(sheet !== -1);
  assert.match(homeHtml.slice(sheet),
    /\.preview-drawer\{left:10px;right:10px;top:auto;bottom:calc\(10px \+ env\(safe-area-inset-bottom\)\)/);
});
