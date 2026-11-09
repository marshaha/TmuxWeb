/* 粘贴板图片的命名规则：剪贴板里的 image.png / blob 不能直接落盘（会互相覆盖）。 */
import {test} from 'node:test';
import assert from 'node:assert';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'web', 'assets', 'composer-files.js'), 'utf8');

function load() {
  const context = {
    window: {},
    document: {addEventListener() {}, createElement: () => ({style: {}, appendChild() {}, setAttribute() {}}),
               querySelector: () => null, getElementById: () => null, body: {addEventListener() {}}},
    URL: {createObjectURL: () => 'blob:x', revokeObjectURL() {}},
    File: class {},
    setTimeout,
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return context.window.TmuxFiles;
}

test('剪贴板默认名换成带时间戳的名字', () => {
  const files = load();
  for (const [type, name] of [['image/png', 'image.png'], ['image/jpeg', 'blob'], ['image/webp', ''], ['image/png', undefined]]) {
    const out = files.pastedNameFor(type, name);
    assert.match(out, /^pasted-\d{8}-\d{6}\.(png|jpg|webp)$/, `${type} / ${name} → ${out}`);
  }
});

test('剪贴板给的扩展名跟类型走', () => {
  const files = load();
  assert.match(files.pastedNameFor('image/jpeg', 'image.png'), /\.jpg$/);
  assert.match(files.pastedNameFor('image/gif', 'blob'), /\.gif$/);
  assert.match(files.pastedNameFor('image/svg+xml', 'image.svg'), /\.svg$/);
});

test('从文件管理器复制的真实文件名保留', () => {
  const files = load();
  assert.equal(files.pastedNameFor('image/png', '屏幕快照 2026-11-09 14.30.12.png'), '屏幕快照 2026-11-09 14.30.12.png');
  assert.equal(files.pastedNameFor('image/png', 'diagram-final.PNG'), 'diagram-final.PNG');
});
