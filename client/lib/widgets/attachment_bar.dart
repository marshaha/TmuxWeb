import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../theme.dart';
import 'path_link_text.dart' show humanSize;

/// One file pending in the chat composer, mirroring web's composer-files.js
/// items: uploaded into `<pane cwd>/upload_files`, shown as a chip while
/// the message is composed, and deleted from the server on removal.
class PendingAttachment {
  PendingAttachment(
      {required this.name,
      required this.bytes,
      required this.isImage,
      this.uploaded = false});

  final String name;
  final List<int> bytes;
  final bool isImage;

  /// False while the PUT is still in flight; the send button refuses to
  /// send while any item is pending, like web's attachmentsPending().
  bool uploaded;

  int get size => bytes.length;
}

/// Web's prompt line, from composer-files.js:144 and index.html's
/// sendInput: the header line, joined with ` & `, in curly quotes.
/// The message sent is `$prompt\n\n$typed`, or just the prompt when
/// nothing was typed.
String attachmentPrompt(List<PendingAttachment> items) {
  final names =
      items.where((item) => item.uploaded).map((item) => item.name).toList();
  return names.isEmpty ? '' : 'user uploaded file “${names.join(' & ')}”';
}

/// Deduplicates like web's uniqueName: `a.png`, `a-2.png`, `a-3.png`, …
String uniqueAttachmentName(String name, Set<String> taken) {
  if (!taken.contains(name)) return name;
  final dot = name.lastIndexOf('.');
  final stem = dot > 0 ? name.substring(0, dot) : name;
  final ext = dot > 0 ? name.substring(dot) : '';
  for (var i = 2; i < 1000; i++) {
    final candidate = '$stem-$i$ext';
    if (!taken.contains(candidate)) return candidate;
  }
  return '$stem-${DateTime.now().millisecondsSinceEpoch}$ext';
}

bool isImageFileName(String name) {
  final lower = name.toLowerCase();
  return lower.endsWith('.png') ||
      lower.endsWith('.jpg') ||
      lower.endsWith('.jpeg') ||
      lower.endsWith('.gif') ||
      lower.endsWith('.webp') ||
      lower.endsWith('.bmp');
}

/// The attachment bar inside the composer card (web's `.cf-bar` +
/// `.cf-strip`, composer-files.css): an attach button with a hint, and
/// one chip per pending file. Images get a 58px thumbnail; other files
/// get an icon + name + size chip. Every chip carries a red 22px disc
/// with a white bold × (composer-files.css:24-25) that removes it.
class AttachmentBar extends StatelessWidget {
  const AttachmentBar({
    super.key,
    required this.items,
    required this.onAttach,
    required this.onRemove,
  });

  final List<PendingAttachment> items;
  final VoidCallback onAttach;
  final void Function(int index) onRemove;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            TextButton.icon(
              key: const Key('chat-attach'),
              onPressed: onAttach,
              style: TextButton.styleFrom(
                minimumSize: const Size(0, 24),
                padding: const EdgeInsets.symmetric(horizontal: 8),
                tapTargetSize: MaterialTapTargetSize.shrinkWrap,
              ),
              icon: const Icon(Icons.attach_file, size: 13),
              label: const Text('附件', style: TextStyle(fontSize: 11)),
            ),
            const Expanded(
              child: Text(
                '存到当前目录的 upload_files',
                textAlign: TextAlign.right,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: 10.5, color: Color(0xFF8F8B82)),
              ),
            ),
          ],
        ),
        if (items.isNotEmpty) ...[
          const SizedBox(height: 2),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (var i = 0; i < items.length; i++)
                _Chip(item: items[i], index: i, onRemove: onRemove),
            ],
          ),
          const SizedBox(height: 8),
        ],
        const Divider(height: 1),
        const SizedBox(height: 6),
      ],
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip(
      {required this.item, required this.index, required this.onRemove});

  final PendingAttachment item;
  final int index;
  final void Function(int index) onRemove;

  @override
  Widget build(BuildContext context) {
    final delete = _DeleteButton(onTap: () => onRemove(index), index: index);
    final body = item.isImage
        ? ClipRRect(
            borderRadius: BorderRadius.circular(10),
            child: Image.memory(
              Uint8List.fromList(item.bytes),
              width: 58,
              height: 58,
              fit: BoxFit.cover,
              errorBuilder: (context, error, stackTrace) => Container(
                width: 58,
                height: 58,
                color: AppTheme.card,
                child: const Icon(Icons.image_outlined, size: 20),
              ),
            ),
          )
        : Container(
            constraints: const BoxConstraints(maxWidth: 230),
            padding: const EdgeInsets.fromLTRB(7, 5, 9, 5),
            decoration: BoxDecoration(
              border: Border.all(color: AppTheme.line),
              borderRadius: BorderRadius.circular(10),
              color: AppTheme.card,
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.description_outlined, size: 14),
                const SizedBox(width: 7),
                Flexible(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(
                        item.name,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 11.5),
                      ),
                      Text(
                        item.uploaded ? humanSize(item.size) : '上传中…',
                        style: TextStyle(fontSize: 10, color: AppTheme.faint),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          );
    return Opacity(
      opacity: item.uploaded ? 1 : 0.55,
      child: Stack(
        clipBehavior: Clip.none,
        children: [
          body,
          Positioned(top: -8, right: -8, child: delete),
        ],
      ),
    );
  }
}

class _DeleteButton extends StatelessWidget {
  const _DeleteButton({required this.onTap, required this.index});

  final VoidCallback onTap;
  final int index;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      key: Key('attachment-remove-$index'),
      onTap: onTap,
      child: Container(
        width: 22,
        height: 22,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: AppTheme.danger,
          border: Border.all(color: Colors.white, width: 2),
          boxShadow: const [
            BoxShadow(
                color: Color(0x52000000), blurRadius: 5, offset: Offset(0, 1))
          ],
        ),
        alignment: Alignment.center,
        child: const Text(
          '×',
          style: TextStyle(
              color: Colors.white,
              fontWeight: FontWeight.bold,
              fontSize: 14,
              height: 1),
        ),
      ),
    );
  }
}
