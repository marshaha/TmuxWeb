import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import '../models/blocks.dart';
import '../models/models.dart';
import '../theme.dart';
import 'block_view.dart';

/// One chat message: user bubbles right, assistant cards left, each with a
/// "copy all" action. Assistant text is markdown; code/table/mermaid blocks get
/// their own toolbars.
class MessageTile extends StatefulWidget {
  const MessageTile({super.key, required this.message, this.onCopied});

  final ChatMessage message;
  final void Function(String text)? onCopied;

  @override
  State<MessageTile> createState() => _MessageTileState();
}

class _MessageTileState extends State<MessageTile> {
  bool _copied = false;

  Future<void> _copyAll() async {
    await Clipboard.setData(ClipboardData(text: widget.message.text));
    widget.onCopied?.call(widget.message.text);
    if (!mounted) return;
    setState(() => _copied = true);
    Future<void>.delayed(const Duration(milliseconds: 1400), () {
      if (mounted) setState(() => _copied = false);
    });
  }

  @override
  Widget build(BuildContext context) {
    final message = widget.message;
    final copyButton = _CopyAllButton(copied: _copied, onTap: _copyAll);

    if (message.isUser) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 7),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Container(
              constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.82),
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              decoration: BoxDecoration(color: const Color(0xFFE9E7DF), borderRadius: BorderRadius.circular(16)),
              child: SelectableText(message.text, style: const TextStyle(fontSize: 14.5, height: 1.45, color: AppTheme.ink)),
            ),
            const SizedBox(height: 4),
            copyButton,
          ],
        ),
      );
    }

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 7),
      child: Container(
        padding: const EdgeInsets.fromLTRB(14, 12, 14, 10),
        decoration: BoxDecoration(
          color: AppTheme.card,
          border: Border.all(color: const Color(0xFFE1DFD7)),
          borderRadius: BorderRadius.circular(16),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            for (final block in splitBlocks(message.text))
              if (block.kind == BlockKind.text)
                MarkdownBody(data: block.text, styleSheet: _markdown, selectable: true)
              else
                BlockView(block: block),
            const Divider(height: 20),
            Align(alignment: Alignment.centerRight, child: copyButton),
          ],
        ),
      ),
    );
  }

  static final _markdown = MarkdownStyleSheet(
    p: const TextStyle(fontSize: 14.5, height: 1.5, color: AppTheme.ink),
    h1: const TextStyle(fontSize: 19, fontWeight: FontWeight.w700, color: AppTheme.ink),
    h2: const TextStyle(fontSize: 17, fontWeight: FontWeight.w700, color: AppTheme.ink),
    h3: const TextStyle(fontSize: 15.5, fontWeight: FontWeight.w600, color: AppTheme.ink),
    listBullet: const TextStyle(fontSize: 14.5, height: 1.5, color: AppTheme.ink),
    blockquote: const TextStyle(fontSize: 14, color: AppTheme.muted, height: 1.5),
    blockquoteDecoration: const BoxDecoration(border: Border(left: BorderSide(color: AppTheme.accent, width: 3))),
    code: const TextStyle(fontFamily: 'monospace', fontSize: 12.5, backgroundColor: Color(0xFFF1EFE7), color: AppTheme.ink),
    strong: const TextStyle(fontWeight: FontWeight.w700, color: AppTheme.ink),
    em: const TextStyle(fontStyle: FontStyle.italic, color: AppTheme.ink),
    a: const TextStyle(color: AppTheme.accent, decoration: TextDecoration.underline),
  );
}

class _CopyAllButton extends StatelessWidget {
  const _CopyAllButton({required this.copied, required this.onTap});

  final bool copied;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(7),
      child: Container(
        height: 26,
        padding: const EdgeInsets.symmetric(horizontal: 10),
        alignment: Alignment.center,
        decoration: BoxDecoration(
          border: Border.all(color: copied ? AppTheme.green : AppTheme.line),
          borderRadius: BorderRadius.circular(7),
          color: const Color(0xE6FFFFFF),
        ),
        child: Text(
          copied ? '已复制' : '复制全部',
          style: TextStyle(fontSize: 11.5, color: copied ? AppTheme.green : AppTheme.muted),
        ),
      ),
    );
  }
}
