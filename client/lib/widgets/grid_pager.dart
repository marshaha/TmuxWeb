import 'dart:async';

import 'package:flutter/material.dart';

/// Fixed ↑ n/N ↓ pager for narrow (≤760px) grid, mirroring web's
/// grid-pager (grid.html:177-191,968-1027): 26px capsules, bottom-right
/// by default (safe-area aware via the parent's padding), a saved
/// position in `tmux-grid-pager-pos` (left/top), long-press to drag it
/// to a new spot, and double-tap on the counter to reset it.
///
/// The pager itself only reports position changes; the parent owns the
/// SharedPreferences read/write so this widget stays testable.
class GridPager extends StatefulWidget {
  const GridPager({
    super.key,
    required this.index,
    required this.total,
    required this.onUp,
    required this.onDown,
    this.savedLeft,
    this.savedTop,
    this.onPositionChanged,
    this.onPositionReset,
  });

  /// 1-based current index, like web's `index + 1`.
  final int index;
  final int total;
  final VoidCallback onUp;
  final VoidCallback onDown;

  /// Saved floating position (logical pixels), or null for the
  /// default bottom-right dock.
  final double? savedLeft;
  final double? savedTop;
  final void Function(double left, double top)? onPositionChanged;
  final VoidCallback? onPositionReset;

  @override
  State<GridPager> createState() => _GridPagerState();
}

class _GridPagerState extends State<GridPager> {
  Offset? _offset;
  Offset? _dragDelta;
  bool _dragging = false;

  /// Web's exact hold (grid.html bindPager: `setTimeout(..., 380)`):
  /// the press only becomes a drag once this timer fires. Flutter's
  /// default long-press recognizer fires later (~500ms) and its
  /// deadline is not web's 380ms, so the timing is driven by an
  /// explicit timer instead of the recognizer.
  static const Duration _holdDuration = Duration(milliseconds: 380);
  Timer? _pressTimer;
  Offset? _pressGlobal;

  @override
  void initState() {
    super.initState();
    _syncSaved();
  }

  @override
  void didUpdateWidget(GridPager oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.savedLeft != oldWidget.savedLeft || widget.savedTop != oldWidget.savedTop) {
      _syncSaved();
    }
  }

  void _syncSaved() {
    if (widget.savedLeft != null && widget.savedTop != null) {
      _offset = Offset(widget.savedLeft!, widget.savedTop!);
    } else {
      _offset = null;
    }
  }

  @override
  void dispose() {
    _pressTimer?.cancel();
    super.dispose();
  }

  void _cancelPress() {
    _pressTimer?.cancel();
    _pressTimer = null;
    _pressGlobal = null;
  }

  void _onPointerDown(PointerDownEvent event) {
    // Web bindPager: any pointerdown on the pager arms the timer;
    // a move before it fires is ignored, and pointerup/cancel
    // disarms it — so a plain tap never enters drag mode.
    _cancelPress();
    _pressGlobal = event.position;
    final screen = MediaQuery.sizeOf(context);
    _pressTimer = Timer(_holdDuration, () {
      _pressTimer = null;
      if (!mounted) return;
      final box = context.findRenderObject() as RenderBox?;
      final origin = box?.localToGlobal(Offset.zero) ?? _pressGlobal!;
      setState(() {
        _dragging = true;
        _dragDelta = _pressGlobal! - origin;
        _offset = _clamped(origin, screen);
      });
    });
  }

  void _onPointerMove(PointerMoveEvent event) {
    if (!_dragging) return;
    final screen = MediaQuery.sizeOf(context);
    setState(() => _offset = _clamped(event.position - (_dragDelta ?? Offset.zero), screen));
  }

  void _onPointerUpOrCancel() {
    _cancelPress();
    if (!_dragging) return;
    final screen = MediaQuery.sizeOf(context);
    final pos = _offset == null ? null : _clamped(_offset!, screen);
    setState(() {
      _dragging = false;
      _dragDelta = null;
      _offset = pos;
    });
    if (pos != null) widget.onPositionChanged?.call(pos.dx, pos.dy);
  }

  Offset _clamped(Offset offset, Size screen) {
    // Pager is roughly 118×36; keep it fully on screen with a margin.
    final maxX = (screen.width - 122).clamp(0.0, double.infinity);
    final maxY = (screen.height - 40).clamp(0.0, double.infinity);
    return Offset(offset.dx.clamp(0.0, maxX), offset.dy.clamp(0.0, maxY));
  }

  @override
  Widget build(BuildContext context) {
    final screen = MediaQuery.sizeOf(context);
    final offset = _offset == null ? null : _clamped(_offset!, screen);
    final pill = Container(
      decoration: BoxDecoration(
        color: Theme.of(context).cardColor,
        border: Border.all(color: Theme.of(context).dividerColor),
        borderRadius: BorderRadius.circular(999),
        boxShadow: [
          if (_dragging) const BoxShadow(color: Colors.black26, blurRadius: 10, offset: Offset(0, 4)),
        ],
      ),
      padding: const EdgeInsets.all(2),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          SizedBox(
            width: 26,
            height: 26,
            child: IconButton(
              key: const Key('grid-pager-up'),
              tooltip: '上一条',
              padding: EdgeInsets.zero,
              iconSize: 12,
              onPressed: widget.index <= 1 ? null : widget.onUp,
              icon: const Icon(Icons.keyboard_arrow_up, size: 12),
            ),
          ),
          GestureDetector(
            onDoubleTap: widget.onPositionReset,
            child: SizedBox(
              width: 34,
              child: Text(
                '${widget.index} / ${widget.total}',
                key: const Key('grid-pager-text'),
                textAlign: TextAlign.center,
                style: const TextStyle(fontSize: 10.5, fontFeatures: [FontFeature.tabularFigures()]),
              ),
            ),
          ),
          SizedBox(
            width: 26,
            height: 26,
            child: IconButton(
              key: const Key('grid-pager-down'),
              tooltip: '下一条',
              padding: EdgeInsets.zero,
              iconSize: 12,
              onPressed: widget.index >= widget.total ? null : widget.onDown,
              icon: const Icon(Icons.keyboard_arrow_down, size: 12),
            ),
          ),
        ],
      ),
    );
    // Raw pointer handling (not GestureDetector long-press): the
    // 380ms timer above is the only thing that enters drag mode,
    // and release persists the position — the same shape as web's
    // bindPager, with web's exact hold. The double-tap reset on the
    // counter keeps its own recognizer inside.
    final draggable = Listener(
      onPointerDown: _onPointerDown,
      onPointerMove: _onPointerMove,
      onPointerUp: (_) => _onPointerUpOrCancel(),
      onPointerCancel: (_) => _onPointerUpOrCancel(),
      child: pill,
    );
    if (offset == null) {
      return Positioned(
        right: 8,
        bottom: 10,
        child: draggable,
      );
    }
    return Positioned(
      left: offset.dx,
      top: offset.dy,
      child: draggable,
    );
  }
}
