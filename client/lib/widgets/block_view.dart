import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../models/blocks.dart';
import '../theme.dart';

/// Per-block toolbar + body: code, tables and mermaid sources each get their own
/// copy button, and tables can flip between rendered and source.
class BlockView extends StatefulWidget {
  const BlockView({super.key, required this.block});

  final Block block;

  @override
  State<BlockView> createState() => _BlockViewState();
}

class _BlockViewState extends State<BlockView> {
  bool _showSource = false;
  bool _copied = false;

  Block get _block => widget.block;

  String get _payload => _block.kind == BlockKind.table ? tableToTsv(parseTable(_block.source)) : _block.source;

  String get _label => switch (_block.kind) {
        BlockKind.code => _block.language,
        BlockKind.mermaid => 'mermaid',
        BlockKind.table => 'table',
        BlockKind.text => 'text',
      };

  Future<void> _copy() async {
    await Clipboard.setData(ClipboardData(text: _payload));
    if (!mounted) return;
    setState(() => _copied = true);
    Future<void>.delayed(const Duration(milliseconds: 1400), () {
      if (mounted) setState(() => _copied = false);
    });
  }

  @override
  Widget build(BuildContext context) {
    final toggleable = _block.kind == BlockKind.table;
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 8),
      decoration: BoxDecoration(
        border: Border.all(color: AppTheme.line),
        borderRadius: BorderRadius.circular(10),
        color: const Color(0xFFFBFAF7),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
            decoration: const BoxDecoration(
              border: Border(bottom: BorderSide(color: AppTheme.line)),
              color: Color(0xB3FFFFFF),
            ),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    _label.toUpperCase(),
                    style: const TextStyle(fontSize: 10, letterSpacing: 0.6, color: AppTheme.faint),
                  ),
                ),
                _BlockButton(
                  label: _copied ? '已复制' : '复制',
                  active: _copied,
                  onTap: _copy,
                  tooltip: _block.kind == BlockKind.table ? '复制为 TSV' : '复制源码',
                ),
                if (toggleable) ...[
                  const SizedBox(width: 6),
                  _BlockButton(
                    label: _showSource ? '渲染' : '源码',
                    onTap: () => setState(() => _showSource = !_showSource),
                    tooltip: '源码 / 渲染',
                  ),
                ],
              ],
            ),
          ),
          if (_block.kind == BlockKind.mermaid)
            const Padding(
              padding: EdgeInsets.fromLTRB(12, 10, 12, 0),
              child: Text(
                'Mermaid 源码（客户端暂不渲染图形，可在网页版查看）',
                style: TextStyle(fontSize: 11, color: AppTheme.faint, height: 1.4),
              ),
            ),
          if (_block.kind == BlockKind.table && !_showSource)
            _TableView(rows: _block.rows)
          else if (_block.kind == BlockKind.table)
            _SourceView(source: _block.source)
          else if (_block.kind == BlockKind.text)
            _SourceView(source: _block.text, mono: false)
          else
            _SourceView(source: _block.source, mono: true),
        ],
      ),
    );
  }
}

class _BlockButton extends StatelessWidget {
  const _BlockButton({required this.label, required this.onTap, this.active = false, this.tooltip = ''});

  final String label;
  final VoidCallback onTap;
  final bool active;
  final String tooltip;

  @override
  Widget build(BuildContext context) {
    final child = InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(6),
      child: Container(
        height: 24,
        padding: const EdgeInsets.symmetric(horizontal: 9),
        alignment: Alignment.center,
        decoration: BoxDecoration(
          border: Border.all(color: active ? AppTheme.green : AppTheme.line),
          borderRadius: BorderRadius.circular(6),
          color: active ? const Color(0x0F4F9A65) : const Color(0xCCFFFFFF),
        ),
        child: Text(label, style: TextStyle(fontSize: 11, color: active ? AppTheme.green : AppTheme.muted)),
      ),
    );
    return tooltip.isEmpty ? child : Tooltip(message: tooltip, child: child);
  }
}

class _SourceView extends StatelessWidget {
  const _SourceView({required this.source, this.mono = false});

  final String source;
  final bool mono;

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: ConstrainedBox(
        constraints: const BoxConstraints(minWidth: 260),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: SelectableText(
            source.trim(),
            style: mono
                ? AppTheme.mono
                : const TextStyle(fontSize: 12, height: 1.5, color: AppTheme.muted, fontFamily: 'monospace'),
          ),
        ),
      ),
    );
  }
}

class _TableView extends StatelessWidget {
  const _TableView({required this.rows});

  final List<List<String>> rows;

  @override
  Widget build(BuildContext context) {
    if (rows.isEmpty) return const SizedBox.shrink();
    final width = rows.fold<int>(0, (value, row) => row.length > value ? row.length : value);

    TableRow buildRow(List<String> cells, {required bool header}) => TableRow(
          decoration: BoxDecoration(color: header ? const Color(0xFFF1EFE7) : null),
          children: List.generate(width, (index) {
            final value = index < cells.length ? cells[index] : '';
            return Padding(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
              child: Text(
                value,
                style: TextStyle(
                  fontSize: 12.5,
                  height: 1.35,
                  color: AppTheme.ink,
                  fontWeight: header ? FontWeight.w600 : FontWeight.w400,
                ),
              ),
            );
          }),
        );

    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: ConstrainedBox(
        constraints: const BoxConstraints(minWidth: 280),
        child: Table(
          defaultColumnWidth: const IntrinsicColumnWidth(),
          border: TableBorder.all(color: const Color(0xFFD1CFC6), width: 0.7),
          children: [
            buildRow(rows.first, header: true),
            ...rows.skip(1).map((row) => buildRow(row, header: false)),
          ],
        ),
      ),
    );
  }
}
