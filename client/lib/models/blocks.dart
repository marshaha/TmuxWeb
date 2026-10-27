/// Chat message blocks: a message body is split into text / fenced code /
/// mermaid / markdown-table blocks so each one can be copied or toggled
/// independently, mirroring the web UI's per-block toolbar.
library;

enum BlockKind { text, code, mermaid, table }

class Block {
  const Block({required this.kind, required this.source, this.language = '', this.text = ''});

  final BlockKind kind;

  /// Raw source, exactly what the copy button puts on the clipboard.
  final String source;
  final String language;

  /// Paragraph text for [BlockKind.text].
  final String text;

  List<List<String>> get rows => kind == BlockKind.table ? parseTable(source) : const [];

  @override
  String toString() => 'Block($kind, ${source.length} chars)';
}

final _fence = RegExp(r'^\s*```\s*([\w+#.-]*)\s*$');
final _tableDivider = RegExp(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$');

bool isTableDivider(String line) => line.contains('-') && _tableDivider.hasMatch(line);

/// Splits a message body into renderable blocks. Fences win over tables, and an
/// unterminated fence runs to the end of the message (agents stream that way).
List<Block> splitBlocks(String body) {
  final lines = body.replaceAll('\r\n', '\n').split('\n');
  final blocks = <Block>[];
  final buffer = <String>[];

  void flushText() {
    final text = buffer.join('\n').trim();
    buffer.clear();
    if (text.isNotEmpty) blocks.add(Block(kind: BlockKind.text, source: text, text: text));
  }

  var index = 0;
  while (index < lines.length) {
    final fence = _fence.firstMatch(lines[index]);
    if (fence != null) {
      flushText();
      final language = fence.group(1)!.toLowerCase();
      final body = <String>[];
      index++;
      while (index < lines.length && _fence.firstMatch(lines[index]) == null) {
        body.add(lines[index]);
        index++;
      }
      index++; // closing fence, or past EOF for a streaming message
      final source = body.join('\n');
      blocks.add(Block(
        kind: language == 'mermaid' ? BlockKind.mermaid : BlockKind.code,
        source: source,
        language: language.isEmpty ? 'code' : language,
      ));
      continue;
    }
    if (index + 1 < lines.length && lines[index].contains('|') && isTableDivider(lines[index + 1])) {
      flushText();
      final table = <String>[lines[index], lines[index + 1]];
      index += 2;
      while (index < lines.length && lines[index].contains('|') && lines[index].trim().isNotEmpty) {
        table.add(lines[index]);
        index++;
      }
      blocks.add(Block(kind: BlockKind.table, source: table.join('\n')));
      continue;
    }
    buffer.add(lines[index]);
    index++;
  }
  flushText();
  return blocks;
}

List<List<String>> parseTable(String source) {
  final rows = <List<String>>[];
  for (final raw in source.split('\n')) {
    final line = raw.trim();
    if (line.isEmpty || isTableDivider(line)) continue;
    var cells = line.replaceAll(r'\|', '\u0000');
    if (cells.startsWith('|')) cells = cells.substring(1);
    if (cells.endsWith('|')) cells = cells.substring(0, cells.length - 1);
    rows.add(cells.split('|').map((cell) => cell.trim().replaceAll('\u0000', '|')).toList());
  }
  return rows;
}

String tableToTsv(List<List<String>> rows) => rows.map((row) => row.join('\t')).join('\n');

String tableToMarkdown(List<List<String>> rows) {
  if (rows.isEmpty) return '';
  final width = rows.fold<int>(0, (value, row) => row.length > value ? row.length : value);
  final head = '| ${rows.first.join(' | ')} |';
  final divider = '| ${List.filled(width, '---').join(' | ')} |';
  return [head, divider, ...rows.skip(1).map((row) => '| ${row.join(' | ')} |')].join('\n');
}
