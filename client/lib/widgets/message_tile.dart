import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import '../models/blocks.dart';
import '../models/models.dart';
import '../theme.dart';
import 'block_view.dart';
import 'path_link_text.dart';

/// One chat message: user bubbles right, assistant cards left, each with a
/// "copy all" action. Assistant text is markdown; code/table/mermaid blocks get
/// their own toolbars.
///
/// With [pathLinks] set, paths in the message are tappable (user bubbles
/// via [PathLinkText], assistant markdown text via a `plink` inline
/// syntax); without it the tile renders exactly as before.
class MessageTile extends StatefulWidget {
  const MessageTile(
      {super.key,
      required this.message,
      this.onCopied,
      this.pathLinks,
      this.baseUrl = '',
      this.cookieHeader = ''});

  final ChatMessage message;
  final void Function(String text)? onCopied;
  final PathLinkActions? pathLinks;

  /// Server base URL + session cookie header, forwarded to BlockView so
  /// mermaid blocks can render (empty = source-only fallback, as in
  /// share screens which pass neither).
  final String baseUrl;
  final String cookieHeader;

  @override
  State<MessageTile> createState() => _MessageTileState();
}

class _MessageTileState extends State<MessageTile> {
  bool _copied = false;

  /// Per-tile fileStat cache for markdown path links, so a link is
  /// stat'ed at most once while this tile is alive.
  final Map<String, Map<String, dynamic>> _statCache = {};

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
    // Web's msg-time, next to the copy row (empty when the server sent none).
    final timestamp = message.timestamp.isEmpty
        ? null
        : Text(message.timestamp,
            style: TextStyle(fontSize: 11, color: AppTheme.muted));

    if (message.isUser) {
      final bubbleStyle =
          TextStyle(fontSize: 14.5, height: 1.45, color: AppTheme.ink);
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 7),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Container(
              constraints: BoxConstraints(
                  maxWidth: MediaQuery.of(context).size.width * 0.82),
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              decoration: BoxDecoration(
                  color: AppTheme.bubble,
                  borderRadius: BorderRadius.circular(16)),
              child: widget.pathLinks == null
                  ? SelectableText(message.text, style: bubbleStyle)
                  : PathLinkText(
                      text: message.text,
                      actions: widget.pathLinks!,
                      style: bubbleStyle),
            ),
            const SizedBox(height: 4),
            Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (timestamp != null) ...[timestamp, const SizedBox(width: 8)],
                copyButton,
              ],
            ),
          ],
        ),
      );
    }

    final pathLinks = widget.pathLinks;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 7),
      child: Container(
        padding: const EdgeInsets.fromLTRB(14, 12, 14, 10),
        decoration: BoxDecoration(
          color: AppTheme.card,
          border: Border.all(color: AppTheme.tileLine),
          borderRadius: BorderRadius.circular(16),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            for (final block in splitBlocks(message.text))
              if (block.kind == BlockKind.text)
                MarkdownBody(
                  data: block.text,
                  styleSheet: _markdown,
                  selectable: true,
                  builders: pathLinks == null
                      ? <String, MarkdownElementBuilder>{}
                      : {
                          'plink': PathLinkElementBuilder(
                              actions: pathLinks, cache: _statCache)
                        },
                  inlineSyntaxes: pathLinks == null ? null : [PathLinkSyntax()],
                )
              else
                BlockView(
                    block: block,
                    baseUrl: widget.baseUrl,
                    cookieHeader: widget.cookieHeader),
            const Divider(height: 20),
            Row(
              children: [
                if (timestamp != null) timestamp,
                const Spacer(),
                copyButton,
              ],
            ),
          ],
        ),
      ),
    );
  }

  static MarkdownStyleSheet get _markdown => MarkdownStyleSheet(
        p: TextStyle(fontSize: 14.5, height: 1.5, color: AppTheme.ink),
        h1: TextStyle(
            fontSize: 19, fontWeight: FontWeight.w700, color: AppTheme.ink),
        h2: TextStyle(
            fontSize: 17, fontWeight: FontWeight.w700, color: AppTheme.ink),
        h3: TextStyle(
            fontSize: 15.5, fontWeight: FontWeight.w600, color: AppTheme.ink),
        listBullet: TextStyle(fontSize: 14.5, height: 1.5, color: AppTheme.ink),
        blockquote: TextStyle(fontSize: 14, color: AppTheme.muted, height: 1.5),
        blockquoteDecoration: BoxDecoration(
            border: Border(left: BorderSide(color: AppTheme.accent, width: 3))),
        code: TextStyle(
            fontFamily: 'monospace',
            fontSize: 12.5,
            backgroundColor: AppTheme.codeBg,
            color: AppTheme.ink),
        strong: TextStyle(fontWeight: FontWeight.w700, color: AppTheme.ink),
        em: TextStyle(fontStyle: FontStyle.italic, color: AppTheme.ink),
        a: TextStyle(
            color: AppTheme.accent, decoration: TextDecoration.underline),
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
          style: TextStyle(
              fontSize: 11.5, color: copied ? AppTheme.green : AppTheme.muted),
        ),
      ),
    );
  }
}
