import 'package:desktop_drop/desktop_drop.dart';
import 'package:flutter/material.dart';

/// The drop-reading seam shared by the main files panel and the
/// sidebar file panel (web's `#file-panel` accepts drops on both
/// surfaces; Flutter's sidebar panel was the missing one).
///
/// Directories arrive as [DropItemDirectory] (desktop_drop fills
/// `children` only for the web drop path; the macOS plugin reports a
/// directory with empty children), so a dropped folder on its own
/// uploads nothing — exactly like web, whose drop handler only reads
/// `dataTransfer.files`. Entries are read in drop order; the first
/// unreadable entry stops the drop with `<name>: <error>`, matching
/// web's `uploadFiles`, which returns on the first failure instead
/// of continuing with the rest.
typedef DroppedUpload = ({String name, List<int> bytes});
typedef DroppedReadFailure = ({String name, Object error});

/// Reads a drop into uploadable pairs, or the first read failure.
/// A failure discards already-read entries: the caller shows
/// `<name>: <error>` and uploads nothing, like web.
Future<({List<DroppedUpload> files, DroppedReadFailure? failure})> readDroppedUploads(
  DropDoneDetails details,
) async {
  final files = <DropItem>[];
  void collect(DropItem item) {
    if (item is DropItemDirectory) {
      for (final child in item.children) {
        collect(child);
      }
    } else {
      files.add(item);
    }
  }

  for (final item in details.files) {
    collect(item);
  }
  final uploads = <DroppedUpload>[];
  for (final file in files) {
    final bookmark = file.extraAppleBookmark;
    var started = false;
    try {
      if (bookmark != null && bookmark.isNotEmpty) {
        started = await DesktopDrop.instance.startAccessingSecurityScopedResource(bookmark: bookmark);
      }
      final bytes = await file.readAsBytes();
      var name = file.name.isNotEmpty ? file.name : file.path.split('/').last;
      if (name.isEmpty) name = '未知文件';
      uploads.add((name: name, bytes: bytes));
    } catch (error) {
      var name = file.name.isNotEmpty ? file.name : file.path.split('/').last;
      if (name.isEmpty) name = '未知文件';
      return (files: <DroppedUpload>[], failure: (name: name, error: error));
    } finally {
      if (started) {
        await DesktopDrop.instance.stopAccessingSecurityScopedResource(bookmark: bookmark!);
      }
    }
  }
  return (files: uploads, failure: null);
}

/// Web's `.file-panel.dragging` affordance: a 2px dashed accent
/// outline over the panel plus a drop hint. Shared by the main files
/// panel and the sidebar file panel; IgnorePointer in the caller's
/// overlay so it can never swallow a tap if the drag state goes
/// stale.
class DashedDropBorderPainter extends CustomPainter {
  const DashedDropBorderPainter(this.color);

  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    const inset = 6.0;
    final rect = Rect.fromLTWH(inset, inset, size.width - inset * 2, size.height - inset * 2);
    final path = Path()..addRRect(RRect.fromRectAndRadius(rect, const Radius.circular(10)));
    final paint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2;
    const dash = 8.0;
    const gap = 6.0;
    for (final metric in path.computeMetrics()) {
      var distance = 0.0;
      while (distance < metric.length) {
        final end = (distance + dash).clamp(0.0, metric.length);
        canvas.drawPath(metric.extractPath(distance, end), paint);
        distance += dash + gap;
      }
    }
  }

  @override
  bool shouldRepaint(DashedDropBorderPainter oldDelegate) => oldDelegate.color != color;
}
