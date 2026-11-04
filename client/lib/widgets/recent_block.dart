import 'dart:async';

import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../theme.dart';

/// One row of the shared "recent" block (web: web/assets/recents.js
/// rowHtml + web/assets/recents.css). Pinned rows render in the tinted
/// pins area above the plain recent rows; the row itself is identical
/// in both mounts — only the click handler differs per page, so the
/// widget takes an [onOpen] callback and never navigates itself.
class RecentRowData {
  const RecentRowData({
    required this.paneRef,
    required this.label,
    this.pinLabel = '',
    this.numberLabel = '',
    this.status = '',
    this.visitedLabel = '',
    this.pinned = false,
    this.starred = false,
  });

  /// Display name, already resolved through nameForPane by the caller.
  final String label;

  /// The pane reference this row opens / pins / stars.
  final String paneRef;

  /// The pin's own label ('' when unpinned); the rename field edits this.
  final String pinLabel;

  /// Web's `#N` short-number prefix, '' when the server assigned none.
  final String numberLabel;

  /// busy | idle | offline | '' (unknown).
  final String status;
  final String visitedLabel;
  final bool pinned;
  final bool starred;
}

class RecentBlockData {
  const RecentBlockData({this.pinned = const [], this.recent = const []});

  final List<RecentRowData> pinned;
  final List<RecentRowData> recent;
}

/// Web's MM-DD HH:MM visited label, shared by the sidebar and home mounts.
String formatRecentVisited(double epochSeconds) {
  if (epochSeconds <= 0) return '';
  final at = DateTime.fromMillisecondsSinceEpoch((epochSeconds * 1000).round());
  String two(int value) => value.toString().padLeft(2, '0');
  return '${two(at.month)}-${two(at.day)} ${two(at.hour)}:${two(at.minute)}';
}

/// One recent row: number, status dot, label, then the per-row actions —
/// pin toggle, pencil rename (pinned rows only), star/favorite, and an
/// optional delete (the home mount keeps its delete action; the sidebar
/// mount has none, like web). Mobile comfort (web recents.css
/// coarse-pointer rules): 44px min row height, 30px always-visible
/// action buttons.
class RecentRow extends StatefulWidget {
  const RecentRow({
    super.key,
    required this.data,
    required this.onOpen,
    required this.onTogglePin,
    required this.onRename,
    required this.onToggleFavorite,
    this.onDelete,
  });

  final RecentRowData data;
  final void Function(String paneRef) onOpen;
  final Future<void> Function(String paneRef, bool pinned) onTogglePin;
  final Future<void> Function(String paneRef, String label) onRename;
  final Future<void> Function(String paneRef, bool starred) onToggleFavorite;
  final Future<void> Function(String paneRef)? onDelete;

  @override
  State<RecentRow> createState() => _RecentRowState();
}

class _RecentRowState extends State<RecentRow> {
  bool _editing = false;
  bool _busy = false;
  late final TextEditingController _controller;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController(text: widget.data.pinLabel);
  }

  @override
  void didUpdateWidget(RecentRow oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!_editing && oldWidget.data.pinLabel != widget.data.pinLabel) {
      _controller.text = widget.data.pinLabel;
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Color _dotColor() => switch (widget.data.status) {
        'busy' => AppTheme.accent,
        'idle' => AppTheme.green,
        'offline' => AppTheme.danger,
        _ => AppTheme.faint,
      };

  Future<void> _run(Future<void> Function() action) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await action();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Widget _action({
    required Key key,
    required String tooltip,
    required IconData icon,
    required bool active,
    required VoidCallback onPressed,
  }) {
    return SizedBox(
      width: 30,
      height: 30,
      child: IconButton(
        key: key,
        tooltip: tooltip,
        padding: EdgeInsets.zero,
        iconSize: 18,
        icon: Icon(icon, color: active ? AppTheme.accent : AppTheme.faint),
        onPressed: _busy ? null : onPressed,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final data = widget.data;
    return Container(
      key: Key('recent-row-${data.paneRef}'),
      constraints: const BoxConstraints(minHeight: 44),
      child: InkWell(
        borderRadius: BorderRadius.circular(7),
        onTap: _editing ? null : () => widget.onOpen(data.paneRef),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
          child: Row(
            children: [
              if (data.numberLabel.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(right: 6),
                  child: Text(data.numberLabel,
                      style: TextStyle(fontSize: 10, color: AppTheme.faint, fontFamily: 'monospace')),
                ),
              Container(
                width: 6,
                height: 6,
                decoration: BoxDecoration(shape: BoxShape.circle, color: _dotColor()),
              ),
              const SizedBox(width: 6),
              if (_editing)
                Expanded(
                  child: TextField(
                    key: Key('recent-rename-input-${data.paneRef}'),
                    controller: _controller,
                    autofocus: true,
                    style: const TextStyle(fontSize: 12),
                    decoration: InputDecoration(
                      isDense: true,
                      contentPadding: const EdgeInsets.symmetric(horizontal: 6, vertical: 8),
                      border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(6), borderSide: BorderSide(color: AppTheme.accent)),
                    ),
                    onSubmitted: (value) {
                      setState(() => _editing = false);
                      _run(() => widget.onRename(data.paneRef, value.trim()));
                    },
                  ),
                )
              else
                Expanded(
                  child: Text(
                    data.label,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                        fontSize: 12, fontWeight: data.pinned ? FontWeight.w600 : FontWeight.w400),
                  ),
                ),
              if (_editing) ...[
                _action(
                  key: Key('recent-rename-save-${data.paneRef}'),
                  tooltip: '保存名字',
                  icon: Icons.check,
                  active: true,
                  onPressed: () {
                    setState(() => _editing = false);
                    _run(() => widget.onRename(data.paneRef, _controller.text.trim()));
                  },
                ),
                _action(
                  key: Key('recent-rename-cancel-${data.paneRef}'),
                  tooltip: '取消改名',
                  icon: Icons.close,
                  active: false,
                  onPressed: () => setState(() {
                    _controller.text = data.pinLabel;
                    _editing = false;
                  }),
                ),
              ] else ...[
                if (data.visitedLabel.isNotEmpty && !data.pinned)
                  Padding(
                    padding: const EdgeInsets.only(right: 2),
                    child: Text(data.visitedLabel, style: TextStyle(fontSize: 10.5, color: AppTheme.faint)),
                  ),
                _action(
                  key: Key('recent-pin-${data.paneRef}'),
                  tooltip: data.pinned ? '取消固定' : '固定到上面',
                  icon: data.pinned ? Icons.push_pin : Icons.push_pin_outlined,
                  active: data.pinned,
                  onPressed: () => _run(() => widget.onTogglePin(data.paneRef, !data.pinned)),
                ),
                if (data.pinned)
                  _action(
                    key: Key('recent-rename-${data.paneRef}'),
                    tooltip: '改名',
                    icon: Icons.edit_outlined,
                    active: false,
                    onPressed: () => setState(() {
                      _controller.text = data.pinLabel;
                      _editing = true;
                    }),
                  ),
                _action(
                  key: Key('recent-star-${data.paneRef}'),
                  tooltip: data.starred ? '取消收藏' : '收藏到主页',
                  icon: data.starred ? Icons.star : Icons.star_border,
                  active: data.starred,
                  onPressed: () => _run(() => widget.onToggleFavorite(data.paneRef, !data.starred)),
                ),
                if (widget.onDelete != null)
                  _action(
                    key: Key('delete-recent-${data.paneRef}'),
                    tooltip: '删除记录',
                    icon: Icons.delete_outline,
                    active: false,
                    onPressed: () => _run(() => widget.onDelete!(data.paneRef)),
                  ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

/// The data-loading recent block: pinned rows in the tinted area plus
/// recent rows, mounted in the hosts sidebar's fixed bottom area (web:
/// recents.css `.recent` with a draggable-height grip and a collapse
/// button; recents.js:516-522 refresh rules).
///
/// Refresh: on visibility/focus return and every 2 minutes, with a 15s
/// dedup window (a reload within 15s of the last one is skipped unless
/// forced, e.g. after a row action). Scroll position survives rebuilds:
/// the offset is saved before the new rows paint and restored after the
/// frame; expanding from collapsed resets it to 0 (recents.js:91).
class RecentBlock extends StatefulWidget {
  const RecentBlock({
    super.key,
    required this.load,
    required this.onOpen,
    required this.onTogglePin,
    required this.onRenamePin,
    required this.onToggleFavorite,
    this.fixedBottom = true,
    this.fillHeight = false,
  });

  final Future<RecentBlockData> Function() load;
  final void Function(String paneRef) onOpen;
  final Future<void> Function(String paneRef, bool pinned) onTogglePin;
  final Future<void> Function(String paneRef, String label) onRenamePin;
  final Future<void> Function(String paneRef, bool starred) onToggleFavorite;

  /// Sidebar mount: fixed bottom area with grip + collapse. False
  /// renders the same rows as a plain shrink-wrapped column.
  final bool fixedBottom;

  /// Only meaningful with fixedBottom false: wrap the row list in an
  /// Expanded so the block can fill a bounded parent (e.g. a drawer
  /// column). Without it the bare ListView inside the min-sized
  /// column gets an unbounded height and fails layout.
  final bool fillHeight;

  static const String heightKey = 'tmux-recent-height';
  static const String collapsedKey = 'tmux-recent-collapsed';

  @override
  State<RecentBlock> createState() => _RecentBlockState();
}

class _RecentBlockState extends State<RecentBlock> with WidgetsBindingObserver {
  RecentBlockData? _data;
  final ScrollController _scroll = ScrollController();
  Timer? _timer;
  DateTime? _lastLoad;
  bool _collapsed = false;
  double _height = 240;
  bool _heightLoaded = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _loadPersisted();
    _reload();
    _timer = Timer.periodic(const Duration(minutes: 2), (_) => _reload());
  }

  Future<void> _loadPersisted() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      if (!mounted) return;
      setState(() {
        _collapsed = prefs.getBool(RecentBlock.collapsedKey) ?? false;
        _height = (prefs.getDouble(RecentBlock.heightKey) ?? 240).clamp(70, 600);
        _heightLoaded = true;
      });
    } catch (_) {}
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _reload();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _reload({bool force = false}) async {
    final now = DateTime.now();
    if (!force && _lastLoad != null && now.difference(_lastLoad!) < const Duration(seconds: 15)) return;
    _lastLoad = now;
    RecentBlockData data;
    try {
      data = await widget.load();
    } catch (_) {
      return; // silent, like web's load catch: stale rows stay on screen
    }
    if (!mounted) return;
    final savedOffset = _scroll.hasClients ? _scroll.offset : 0.0;
    setState(() => _data = data);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      final max = _scroll.position.maxScrollExtent;
      _scroll.jumpTo(savedOffset.clamp(0.0, max));
    });
  }

  Future<void> _afterAction(Future<void> Function() action) async {
    try {
      await action();
    } catch (_) {}
    await _reload(force: true);
  }

  Future<void> _toggleCollapsed() async {
    final next = !_collapsed;
    setState(() => _collapsed = next);
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool(RecentBlock.collapsedKey, next);
    } catch (_) {}
    if (!next) {
      // Expanding resets the scroll to the top (recents.js:91): the
      // collapsed period hid the pins, and restoring the old offset
      // would push them out of view.
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (_scroll.hasClients) _scroll.jumpTo(0);
      });
    }
  }

  Widget _rows() {
    final data = _data;
    if (data == null) return const SizedBox.shrink();
    RecentRow row(RecentRowData item) => RecentRow(
          key: ValueKey('recent-${item.paneRef}'),
          data: item,
          onOpen: widget.onOpen,
          onTogglePin: (ref, pinned) => _afterAction(() => widget.onTogglePin(ref, pinned)),
          onRename: (ref, label) => _afterAction(() => widget.onRenamePin(ref, label)),
          onToggleFavorite: (ref, starred) => _afterAction(() => widget.onToggleFavorite(ref, starred)),
        );
    return ListView(
      key: const Key('recent-list'),
      controller: _scroll,
      padding: EdgeInsets.only(bottom: MediaQuery.of(context).padding.bottom),
      children: [
        if (data.pinned.isNotEmpty)
          Container(
            padding: const EdgeInsets.all(2),
            decoration: BoxDecoration(
              color: AppTheme.green.withValues(alpha: 0.14),
              border: Border.all(color: AppTheme.green.withValues(alpha: 0.22)),
              borderRadius: BorderRadius.circular(9),
            ),
            child: Column(children: [for (final item in data.pinned) row(item)]),
          ),
        if (data.pinned.isNotEmpty && data.recent.isNotEmpty) const SizedBox(height: 5),
        for (final item in data.recent) row(item),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final data = _data;
    final count = (data?.pinned.length ?? 0) + (data?.recent.length ?? 0);
    final header = Row(
      children: [
        Expanded(
          child: Text('最近 $count',
              style: TextStyle(fontSize: 10.5, letterSpacing: 0.06, color: AppTheme.faint)),
        ),
        IconButton(
          key: const Key('recent-fold'),
          tooltip: _collapsed ? '展开最近' : '折叠最近',
          iconSize: 18,
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 26, minHeight: 26),
          icon: Icon(_collapsed ? Icons.keyboard_arrow_up : Icons.keyboard_arrow_down,
              size: 18, color: AppTheme.faint),
          onPressed: _toggleCollapsed,
        ),
      ],
    );
    if (!widget.fixedBottom) {
      return Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          header,
          if (!_collapsed) (widget.fillHeight ? Expanded(child: _rows()) : _rows()),
        ],
      );
    }
    if (_collapsed) {
      return Container(
        padding: const EdgeInsets.fromLTRB(6, 6, 6, 4),
        decoration: BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
        child: header,
      );
    }
    return Container(
      height: _heightLoaded ? _height : 240,
      padding: const EdgeInsets.fromLTRB(6, 6, 6, 4),
      decoration: BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // Draggable-height grip (recents.css .recent-grip): drag up
          // to grow the block, down to shrink it; persisted locally.
          GestureDetector(
            key: const Key('recent-grip'),
            onVerticalDragUpdate: (details) {
              setState(() => _height = (_height - details.delta.dy).clamp(70, 600));
            },
            onVerticalDragEnd: (_) async {
              try {
                final prefs = await SharedPreferences.getInstance();
                await prefs.setDouble(RecentBlock.heightKey, _height);
              } catch (_) {}
            },
            child: Container(
              height: 10,
              color: Colors.transparent,
              alignment: Alignment.center,
              child: Container(
                width: 28,
                height: 2,
                decoration: BoxDecoration(color: AppTheme.faint, borderRadius: BorderRadius.circular(2)),
              ),
            ),
          ),
          header,
          Expanded(child: _rows()),
        ],
      ),
    );
  }
}
