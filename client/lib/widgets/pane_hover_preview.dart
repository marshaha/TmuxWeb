import 'dart:async';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../theme.dart';

/// 一条预览：谁说的、什么时候、正文。
class PanePreviewData {
  const PanePreviewData({required this.role, required this.text, this.time});

  final String role;
  final String text;
  final String? time;

  String get roleLabel => role == 'assistant' ? 'agent 回复' : role;
}

/// 侧栏一行的「悬停预览」：指针停住 [delay] 之后浮出最后一条消息。
///
/// 和 Web 端是同一套交互（Web 共用 `/assets/pane-preview.js`）：手机没有悬停，
/// 因此只在鼠标进入时才计时；`tmux-preview-delay` 与 Web 共用同一个键
/// （off / 1 / 2 / 3 秒），读不到就用 2 秒。
class PaneHoverPreview extends StatefulWidget {
  const PaneHoverPreview({
    super.key,
    required this.paneRef,
    required this.loadPreview,
    required this.child,
    this.delay,
  });

  final String paneRef;

  /// 取这条会话的预览；返回 null 表示没有内容（不弹空面板）。
  final Future<PanePreviewData?> Function(String paneRef) loadPreview;
  final Widget child;

  /// 显式指定就用它，否则按设置（默认 2 秒，0 = 关闭）。
  final Duration? delay;

  /// 设置里存的是秒：off / 1 / 2 / 3。
  static const String delayKey = 'tmux-preview-delay';

  static Duration durationFromPref(String? raw) {
    if (raw == null || raw.isEmpty) return const Duration(seconds: 2);
    if (raw == 'off') return Duration.zero;
    final seconds = int.tryParse(raw);
    if (seconds == null || seconds < 0) return const Duration(seconds: 2);
    return Duration(seconds: seconds);
  }

  static Future<Duration> loadDelay() async {
    final prefs = await SharedPreferences.getInstance();
    return durationFromPref(prefs.getString(delayKey));
  }

  @override
  State<PaneHoverPreview> createState() => _PaneHoverPreviewState();
}

class _PaneHoverPreviewState extends State<PaneHoverPreview> {
  Timer? _timer;
  OverlayEntry? _entry;
  bool _busy = false;

  @override
  void dispose() {
    _cancel();
    super.dispose();
  }

  void _cancel() {
    _timer?.cancel();
    _timer = null;
    _entry?.remove();
    _entry = null;
  }

  Future<void> _fire(String paneRef) async {
    if (!mounted || _busy || _entry != null) return;
    _busy = true;
    PanePreviewData? data;
    try {
      data = await widget.loadPreview(paneRef);
    } catch (_) {
      data = null;
    } finally {
      _busy = false;
    }
    if (!mounted || data == null || _entry != null) return;
    final preview = data;                     // 上面已经判过 null
    final box = context.findRenderObject();
    if (box is! RenderBox || !box.hasSize) return;
    final origin = box.localToGlobal(Offset.zero);
    final media = MediaQuery.of(context);
    final width = (media.size.width - 24).clamp(220.0, 420.0);
    // 侧栏在左边：面板放在行右侧；空间不够就退回左侧
    final rightSide = origin.dx + box.size.width + width + 24 < media.size.width;
    final left = rightSide
        ? origin.dx + box.size.width + 12
        : (origin.dx - width - 12).clamp(12.0, media.size.width - width - 12);
    final top = origin.dy.clamp(12.0, (media.size.height - 220).clamp(12.0, double.infinity));
    final entry = OverlayEntry(
      builder: (context) => Positioned(
        left: left.toDouble(),
        top: top.toDouble(),
        width: width.toDouble(),
        child: IgnorePointer(
          child: Material(
            color: AppTheme.card,
            elevation: 10,
            borderRadius: BorderRadius.circular(12),
            child: Container(
              key: const Key('pane-hover-preview'),
              padding: const EdgeInsets.fromLTRB(12, 10, 12, 12),
              decoration: BoxDecoration(
                border: Border.all(color: AppTheme.line),
                borderRadius: BorderRadius.circular(12),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Row(
                    children: [
                      Text(preview.roleLabel, style: TextStyle(fontSize: 10.5, color: AppTheme.muted)),
                      if (preview.time != null) ...[
                        const SizedBox(width: 8),
                        Text(preview.time!, style: TextStyle(fontSize: 10.5, color: AppTheme.faint)),
                      ],
                    ],
                  ),
                  const SizedBox(height: 6),
                  ConstrainedBox(
                    constraints: const BoxConstraints(maxHeight: 240),
                    child: SingleChildScrollView(
                      child: Text(
                        preview.text,
                        style: TextStyle(fontSize: 12.5, height: 1.5, color: AppTheme.ink),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
    _entry = entry;
    Overlay.of(context, rootOverlay: true).insert(entry);
  }

  Future<void> _enter(PointerEnterEvent event) async {
    if (event.kind != PointerDeviceKind.mouse) return;   // 触摸没有悬停
    final delay = widget.delay ?? await PaneHoverPreview.loadDelay();
    if (delay <= Duration.zero) return;
    _timer?.cancel();
    final paneRef = widget.paneRef;
    _timer = Timer(delay, () => _fire(paneRef));
  }

  void _leave(PointerExitEvent event) {
    if (event.kind != PointerDeviceKind.mouse) return;
    _cancel();
  }

  @override
  Widget build(BuildContext context) {
    return MouseRegion(
      onEnter: _enter,
      onExit: _leave,
      child: widget.child,
    );
  }
}
