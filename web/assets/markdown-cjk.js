/* CommonMark 的「右围栏」规则会让中文加粗失效。
 *
 *   **任务：**它停了。      ← 不渲染！（闭合 ** 前面是标点「：」、后面紧跟汉字）
 *   **任务：** 它停了。     ← 渲染（后面是空格）
 *   **Task:**it stopped.   ← 渲染（后面是字母，但前面不是中文标点…）
 *
 * 规则原文：闭合分隔符必须是 right-flanking —— 前面不能是空白，且「前面不是标点、
 * 或者后面是空白/标点」。`：` 是标点、`它` 是字母，两头都不满足，于是整对 ** 变成
 * 普通字符。中文写作里 `**加粗：**后面直接接正文` 太常见了，所以这里做一次小手术：
 *
 *   1. 解析前，在「前面是标点、后面紧跟非空白」的闭合 ** 前插一个零宽空格，
 *      让它变成 right-flanking；
 *   2. 解析后，把所有零宽空格删掉（它只在解析期起作用，不该出现在正文里）。
 *
 * 只动 `**...**`，不碰单星号斜体；代码块里的内容解析后照旧，零宽空格也会被删掉。
 */
(function () {
  const ZERO_WIDTH = '\u200B';
  // 闭合 ** 的三种前提：正文里出现标点、紧跟一个非空白字符
  const CLOSER_RE = /(\*\*)([^*\n]{1,600}?)([：:，,。.、；;！!？?…—)\]）】」』》"'\u2019\u201d])(\*\*)(?=[^\s])/g;

  function fix(text) {
    return String(text == null ? '' : text).replace(
      CLOSER_RE,
      (_match, open, body, punctuation, close) => open + body + punctuation + ZERO_WIDTH + close,
    );
  }

  function strip(html) {
    return String(html == null ? '' : html).split(ZERO_WIDTH).join('');
  }

  window.TmuxMarkdownCjk = {fix, strip, ZERO_WIDTH, CLOSER_RE};
})();
