/* 打包脚本的语法闸门。
 *
 * 这些文件是直接拼进 console.bundle.js 的：任何一处语法错都会让**整个** bundle 不执行
 * （页面看着"没报错"，其实 TmuxQueue 之类全是 undefined）。真实发生过一次：
 * 一次替换把 `$('selected-sub').textContent=` 的尾巴吃掉，`node --check` 还报 exit 0。
 *
 * 所以这里用 new Function 真解析一遍（去掉 import/export 语句，浏览器里它们是模块语法）。
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FILES = [
  'web/assets/pane-cache.js',
  'web/assets/uploads.js',
  'web/assets/composer-files.js',
  'web/assets/path-links.js',
  'web/assets/prompt-card.js',
  'web/assets/agents-panel.js',
  'web/assets/perf-budget.js',
  'web/assets/needs-you.js',
  'web/assets/markdown-cjk.js',
  'web/assets/video-cards.js',
  'web/assets/upload-cards.js',
  'web/assets/image-viewer.js',
  'web/assets/transfer.js',
  'web/assets/console-app.js',
];

for (const file of FILES) {
  test(`${file} parses`, () => {
    const source = readFileSync(join(root, file), 'utf8')
      .split('\n')
      .filter(line => !/^\s*(import|export)\b/.test(line))
      .join('\n');
    assert.doesNotThrow(() => new Function(source), `${file} 有语法错误，bundle 会整段不执行`);
  });
}

test('the console exports the modules the page relies on', () => {
  const source = readFileSync(join(root, 'web/assets/console-app.js'), 'utf8');
  for (const marker of ['window.TmuxQueue', 'window.TmuxLoadCard', 'window.TmuxAgents',
                        'window.TmuxPerf', 'window.TmuxTurn']) {
    assert.match(source, new RegExp(marker.replace('.', '\\.')), `${marker} 不见了`);
  }
});
