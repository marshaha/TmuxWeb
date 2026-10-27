import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/models/blocks.dart';

void main() {
  group('splitBlocks', () {
    test('keeps plain text as one text block', () {
      final blocks = splitBlocks('第一段\n\n第二段');
      expect(blocks, hasLength(1));
      expect(blocks.single.kind, BlockKind.text);
      expect(blocks.single.text, '第一段\n\n第二段');
    });

    test('detects a fenced code block with its language', () {
      final blocks = splitBlocks('说明\n\n```python\nprint("hi")\n```\n\n结尾');
      expect(blocks.map((block) => block.kind), [BlockKind.text, BlockKind.code, BlockKind.text]);
      expect(blocks[1].language, 'python');
      expect(blocks[1].source, 'print("hi")');
    });

    test('keeps code verbatim including <, > and &', () {
      final blocks = splitBlocks('```python\nif a < b and c > d:\n    print("x & y")\n```');
      expect(blocks.single.source, 'if a < b and c > d:\n    print("x & y")');
    });

    test('flags mermaid fences', () {
      final blocks = splitBlocks('```mermaid\ngraph TD\n  A --> B\n```');
      expect(blocks.single.kind, BlockKind.mermaid);
      expect(blocks.single.source, 'graph TD\n  A --> B');
    });

    test('treats an unterminated fence as code to the end', () {
      final blocks = splitBlocks('```sh\nls -la');
      expect(blocks.single.kind, BlockKind.code);
      expect(blocks.single.source, 'ls -la');
    });

    test('finds a markdown table between paragraphs', () {
      final blocks = splitBlocks('前\n\n| 主机 | pane |\n| --- | --- |\n| ctyun | %0 |\n\n后');
      expect(blocks.map((block) => block.kind), [BlockKind.text, BlockKind.table, BlockKind.text]);
      expect(blocks[1].source.split('\n').length, 3);
    });

    test('does not mistake a pipe sentence for a table', () {
      expect(splitBlocks('用 a | b 表示或').single.kind, BlockKind.text);
    });

    test('handles several blocks in one message', () {
      final blocks = splitBlocks('说明\n```js\nlet a = 1\n```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```mermaid\ngraph TD\n```');
      expect(blocks.map((block) => block.kind),
          [BlockKind.text, BlockKind.code, BlockKind.table, BlockKind.mermaid]);
    });
  });

  group('table helpers', () {
    const source = '| 主机 | pane | 状态 |\n| --- | --- | --- |\n| ctyun | %0 | idle |\n| robo | %7 | busy |';

    test('parses header and rows without the divider', () {
      final rows = parseTable(source);
      expect(rows, hasLength(3));
      expect(rows.first, ['主机', 'pane', '状态']);
      expect(rows.last, ['robo', '%7', 'busy']);
    });

    test('copies as TSV', () {
      expect(tableToTsv(parseTable(source)).split('\n').first, '主机\tpane\t状态');
    });

    test('re-renders markdown with a divider of the right width', () {
      expect(tableToMarkdown(parseTable(source)).split('\n')[1], '| --- | --- | --- |');
    });

    test('handles short rows and escaped pipes', () {
      final rows = parseTable('| a | b |\n| --- | --- |\n| only |\n| x \\| y | z |');
      expect(rows[1], ['only']);
      expect(rows[2], ['x | y', 'z']);
    });
  });
}
