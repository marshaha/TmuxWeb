import 'dart:async';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../theme.dart';
import 'pane_hover_preview.dart';

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
    this.active = false,
  });

  /// Web's .active row (recents.js markActive): the selected pane's row.
  final bool active;

  RecentRowData copyWith({String? status, bool? active}) => RecentRowData(
        paneRef: paneRef,
        label: label,
        pinLabel: pinLabel,
        numberLabel: numberLabel,
        status: status ?? this.status,
        visitedLabel: visitedLabel,
        pinned: pinned,
        starred: starred,
        active: active ?? this.active,
      );

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

/// Web's relative visited label (recents.js relativeTime), shared by the
/// sidebar and home mounts: `刚刚` / `N 分钟` / `N 小时` / `N 天`, with
/// no suffix. A stamp at or after `now` (or <= 0) renders as '' — web
/// returns '' when the rounded second delta is 0. Pinned rows never
/// show a time at all (RecentRow suppresses it); callers also pass ''.
String formatRecentVisited(double epochSeconds, {DateTime? now}) {
  if (epochSeconds <= 0) return '';
  final current = now ?? DateTime.now();
  final seconds =
      (current.millisecondsSinceEpoch / 1000 - epochSeconds).round();
  if (seconds <= 0) return '';
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return '${seconds ~/ 60} 分钟';
  if (seconds < 86400) return '${seconds ~/ 3600} 小时';
  return '${seconds ~/ 86400} 天';
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
    _controller = TextEditingController(text: _initialText());
    _focusNode = FocusNode();
    _focusNode.addListener(_onFocusChange);
  }

  bool _finished = true;
  PointerDeviceKind? _lastPointerKind;
  late final FocusNode _focusNode;

  /// Web startRename initial text (recents.js:349-390): the pin's own
  /// label, falling back to the displayed label when the pin has none.
  String _initialText() {
    final pin = widget.data.pinLabel.trim();
    if (pin.isNotEmpty) return pin;
    return widget.data.label.trim();
  }

  @override
  void didUpdateWidget(RecentRow oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!_editing &&
        (oldWidget.data.pinLabel != widget.data.pinLabel ||
            oldWidget.data.label != widget.data.label)) {
      _controller.text = _initialText();
    }
  }

  @override
  void dispose() {
    _focusNode.removeListener(_onFocusChange);
    _focusNode.dispose();
    _controller.dispose();
    super.dispose();
  }

  /// Blur saves (web's blur handler). Enter/Escape finish first and set
  /// [_finished], so this is a no-op for them.
  void _onFocusChange() {
    if (!_focusNode.hasFocus && _editing && !_finished) _finish(save: true);
  }

  /// Pinned rows only (web startRename refuses unpinned rows).
  void _startEdit() {
    if (!widget.data.pinned || _editing) return;
    setState(() {
      _controller.text = _initialText();
      _editing = true;
      _finished = false;
    });
  }

  void _finish({required bool save}) {
    if (_finished) return;
    _finished = true;
    final raw = _controller.text.trim();
    final clean = raw.length > 80 ? raw.substring(0, 80) : raw;
    setState(() {
      _editing = false;
      if (!save) _controller.text = _initialText();
    });
    _focusNode.unfocus();
    if (save) _run(() => widget.onRename(widget.data.paneRef, clean));
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
    // Web .active (recents.css): the selected pane's row gets the accent
    // tint.
    final row = Container(
      key: Key('recent-row-${data.paneRef}'),
      constraints: const BoxConstraints(minHeight: 44),
      decoration: data.active
          ? BoxDecoration(
              color: AppTheme.accent.withValues(alpha: 0.10),
              borderRadius: BorderRadius.circular(7),
            )
          : null,
      // Web's row is an open button (.recent-open) plus sibling action
      // buttons. The tap / double-tap / long-press recognizers live on
      // the open area only: an InkWell with onDoubleTap wrapping the
      // whole row holds the gesture arena and swallows the pin/star
      // buttons' taps (verified in a scratch test), so the actions must
      // stay siblings of the open area, not children of its InkWell.
      child: Row(
        children: [
          Expanded(
            // Web rename entry (recents.js:349-390): double-click on the
            // open area (desktop) and 480ms touch long-press. LongPress
            // details carry no pointer kind, so the Listener records the
            // kind of the pointer that went down on this row.
            child: Listener(
              onPointerDown: (event) => _lastPointerKind = event.kind,
              child: GestureDetector(
                onLongPress: () {
                  if (data.pinned &&
                      _lastPointerKind == PointerDeviceKind.touch) {
                    _startEdit();
                  }
                },
                child: InkWell(
                  borderRadius: BorderRadius.circular(7),
                  onTap: _editing ? null : () => widget.onOpen(data.paneRef),
                  onDoubleTap: data.pinned && !_editing ? _startEdit : null,
                  child: Padding(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
                    child: Row(
                      children: [
                        if (data.numberLabel.isNotEmpty)
                          Padding(
                            padding: const EdgeInsets.only(right: 6),
                            child: Text(data.numberLabel,
                                style: TextStyle(
                                    fontSize: 10,
                                    color: AppTheme.faint,
                                    fontFamily: 'monospace')),
                          ),
                        Container(
                          width: 6,
                          height: 6,
                          decoration: BoxDecoration(
                              shape: BoxShape.circle, color: _dotColor()),
                        ),
                        const SizedBox(width: 6),
                        if (_editing)
                          Expanded(
                            // No explicit save/cancel buttons (web: Enter saves,
                            // Escape cancels, blur saves). Escape is caught here;
                            // blur goes through the FocusNode listener.
                            child: Focus(
                              onKeyEvent: (node, event) {
                                if (event is KeyDownEvent &&
                                    event.logicalKey ==
                                        LogicalKeyboardKey.escape) {
                                  _finish(save: false);
                                  return KeyEventResult.handled;
                                }
                                return KeyEventResult.ignored;
                              },
                              child: TextField(
                                key: Key('recent-rename-input-${data.paneRef}'),
                                controller: _controller,
                                focusNode: _focusNode,
                                autofocus: true,
                                maxLength: 80,
                                maxLengthEnforcement:
                                    MaxLengthEnforcement.enforced,
                                buildCounter: (_,
                                        {required currentLength,
                                        required isFocused,
                                        required maxLength}) =>
                                    const SizedBox.shrink(),
                                style: const TextStyle(fontSize: 12),
                                decoration: InputDecoration(
                                  isDense: true,
                                  contentPadding: const EdgeInsets.symmetric(
                                      horizontal: 6, vertical: 8),
                                  border: OutlineInputBorder(
                                      borderRadius: BorderRadius.circular(6),
                                      borderSide:
                                          BorderSide(color: AppTheme.accent)),
                                ),
                                onSubmitted: (_) => _finish(save: true),
                              ),
                            ),
                          )
                        else
                          Expanded(
                            child: Text(
                              data.label,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                  fontSize: 12,
                                  fontWeight: data.pinned
                                      ? FontWeight.w600
                                      : FontWeight.w400),
                            ),
                          ),
                        if (data.visitedLabel.isNotEmpty && !data.pinned)
                          Padding(
                            padding: const EdgeInsets.only(right: 2),
                            child: Text(data.visitedLabel,
                                style: TextStyle(
                                    fontSize: 10.5, color: AppTheme.faint)),
                          ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
          // Web keeps the row actions during a rename; the blur
          // save above commits a pending edit before an action's
          // onPressed runs, like web's pointerdown commit.
          _action(
            key: Key('recent-pin-${data.paneRef}'),
            tooltip: data.pinned ? '取消固定' : '固定到上面',
            icon: data.pinned ? Icons.push_pin : Icons.push_pin_outlined,
            active: data.pinned,
            onPressed: () =>
                _run(() => widget.onTogglePin(data.paneRef, !data.pinned)),
          ),
          if (data.pinned)
            _action(
              key: Key('recent-rename-${data.paneRef}'),
              tooltip: '改名',
              icon: Icons.edit_outlined,
              active: false,
              onPressed: _startEdit,
            ),
          _action(
            key: Key('recent-star-${data.paneRef}'),
            tooltip: data.starred ? '取消收藏' : '收藏到主页',
            icon: data.starred ? Icons.star : Icons.star_border,
            active: data.starred,
            onPressed: () => _run(
                () => widget.onToggleFavorite(data.paneRef, !data.starred)),
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
      ),
    );
    if (!data.pinned) return row;
    // Web suppresses the context menu on pinned rows (recents.js:517-519).
    // Flutter has no default row menu, so the secondary-tap gesture is
    // claimed here so no ancestor menu can open over a pinned row either.
    return GestureDetector(
      key: Key('recent-row-context-suppress-${data.paneRef}'),
      onSecondaryTap: () {},
      child: row,
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
    this.activePaneRef,
    this.hideWhenFiles = false,
    this.isFilesPanelOpen = false,
    this.knownPaneRefs,
    this.sidebarHeight,
    this.onClearUnpinned,
    this.onRenameFavoriteTitle,
    this.loadPreview,
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

  /// Web markActive (recents.js:305-312): the selected pane's ref; its
  /// row renders .active. Null/empty means no active pane, which also
  /// disables the pin-selected header button.
  final String? activePaneRef;

  /// Web console mount only (recents.js hideWhenFiles): when true and
  /// [isFilesPanelOpen], the whole block hides. Other callers leave
  /// both at their defaults and are unaffected.
  final bool hideWhenFiles;
  final bool isFilesPanelOpen;

  /// Web render filtering (recents.js:144-156): when non-null, unpinned
  /// rows whose pane is not in this set are dropped; pinned rows for
  /// vanished panes stay with an offline dot. Null keeps the old
  /// behavior (all rows shown) for callers that do not pass the tree.
  final Set<String>? knownPaneRefs;

  /// The sidebar's height, for the web ratio height model (recents.js
  /// --recent-h): block height = ratio x sidebar height. Null falls
  /// back to [_fallbackSidebarHeight] so existing callers don't break.
  final double? sidebarHeight;

  /// Web recent-clear (recents.js:502-516): delete all unpinned rows.
  /// The caller deletes each ref (DELETE /api/home/recent/<pane>);
  /// the button is disabled until a caller wires this in.
  final Future<void> Function(List<String> paneRefs)? onClearUnpinned;

  /// Web rename side effect (recents.js:349-390): renaming a favorited
  /// pin also updates its favorite title. The caller PATCHes the
  /// favorite title; without it the title goes stale on rename.
  final Future<void> Function(String paneRef, String label)?
      onRenameFavoriteTitle;

  /// Hover preview (web index.html setupPanePreview): when non-null,
  /// rows are wrapped in the shared [PaneHoverPreview] with the same
  /// tmux-preview-delay key. Null renders rows unwrapped.
  final Future<PanePreviewData?> Function(String paneRef)? loadPreview;

  /// Web height model (recents.js:10-11): ratio of sidebar height.
  static const String heightKey = 'tmux-recent-h';
  static const String collapsedKey = 'tmux-recent-fold';
  static const String _legacyHeightKey = 'tmux-recent-height';
  static const String _legacyCollapsedKey = 'tmux-recent-collapsed';
  static const double heightRatioDefault = 0.4;
  static const double heightRatioMin = 0.15;
  static const double heightRatioMax = 0.75;
  static const double fallbackSidebarHeight = 600;
  static const int maxUnpinnedRows = 8;

  @override
  State<RecentBlock> createState() => _RecentBlockState();
}

class _RecentBlockState extends State<RecentBlock> with WidgetsBindingObserver {
  RecentBlockData? _data;
  final ScrollController _scroll = ScrollController();
  Timer? _timer;
  DateTime? _lastLoad;
  bool _collapsed = false;
  double _ratio = RecentBlock.heightRatioDefault;
  bool _heightLoaded = false;

  /// Web ratio height: block height = ratio x sidebar height.
  double get _totalHeight {
    final given = widget.sidebarHeight;
    if (given != null && given > 0) return given;
    return RecentBlock.fallbackSidebarHeight;
  }

  double get _heightPx => _ratio * _totalHeight;

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
      // Fold key migration (E12): read the new key, fall back to the
      // old one once, then write only the new key from here on.
      var collapsed = prefs.getBool(RecentBlock.collapsedKey);
      final legacyCollapsed = prefs.getBool(RecentBlock._legacyCollapsedKey);
      if (collapsed == null && legacyCollapsed != null) {
        collapsed = legacyCollapsed;
        await prefs.setBool(RecentBlock.collapsedKey, legacyCollapsed);
      }
      // Height migration (E11): new key is a ratio; the old key was px,
      // converted against the sidebar height and then persisted as a
      // ratio so the old key is never read again after this load.
      var ratio = prefs.getDouble(RecentBlock.heightKey);
      if (ratio == null) {
        final legacyPx = prefs.getDouble(RecentBlock._legacyHeightKey);
        if (legacyPx != null) {
          ratio = (legacyPx / _totalHeight)
              .clamp(RecentBlock.heightRatioMin, RecentBlock.heightRatioMax);
          await prefs.setDouble(RecentBlock.heightKey, ratio);
        }
      }
      if (!mounted) return;
      setState(() {
        _collapsed = collapsed ?? false;
        _ratio = (ratio ?? RecentBlock.heightRatioDefault)
            .clamp(RecentBlock.heightRatioMin, RecentBlock.heightRatioMax);
        _heightLoaded = true;
      });
    } catch (_) {}
  }

  /// The rows web would render (recents.js render): out-of-tree
  /// unpinned rows dropped, pinned vanished rows kept with an offline
  /// dot, unpinned rows capped at MAX_ROWS=8 (pinned uncapped), and the
  /// active flag applied. Null until the first load completes.
  RecentBlockData? get _visibleData {
    final data = _data;
    if (data == null) return null;
    final known = widget.knownPaneRefs;
    List<RecentRowData> filter(List<RecentRowData> rows) {
      if (known == null) return rows;
      return [
        for (final item in rows)
          if (item.pinned || known.contains(item.paneRef))
            item.pinned && !known.contains(item.paneRef)
                ? item.copyWith(status: 'offline')
                : item,
      ];
    }

    final active = widget.activePaneRef;
    RecentRowData mark(RecentRowData item) => item.copyWith(
        active: active != null && active.isNotEmpty && item.paneRef == active);
    return RecentBlockData(
      pinned: [for (final item in filter(data.pinned)) mark(item)],
      recent: [
        for (final item
            in filter(data.recent).take(RecentBlock.maxUnpinnedRows))
          mark(item),
      ],
    );
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
    if (!force &&
        _lastLoad != null &&
        now.difference(_lastLoad!) < const Duration(seconds: 15)) {
      return;
    }
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
    final data = _visibleData;
    if (data == null) return const SizedBox.shrink();
    Widget row(RecentRowData item) {
      final built = RecentRow(
        key: ValueKey('recent-${item.paneRef}'),
        data: item,
        onOpen: widget.onOpen,
        onTogglePin: (ref, pinned) =>
            _afterAction(() => widget.onTogglePin(ref, pinned)),
        onRename: (ref, label) => _afterAction(() async {
          await widget.onRenamePin(ref, label);
          // Web rename side effect: a favorited pin's favorite title
          // follows the new label (recents.js:349-390).
          if (item.starred) {
            await widget.onRenameFavoriteTitle?.call(ref, label);
          }
        }),
        onToggleFavorite: (ref, starred) =>
            _afterAction(() => widget.onToggleFavorite(ref, starred)),
      );
      // Hover preview (web: bound to recent rows too, same delay key).
      final loadPreview = widget.loadPreview;
      if (loadPreview == null) return built;
      return PaneHoverPreview(
        paneRef: item.paneRef,
        loadPreview: loadPreview,
        child: built,
      );
    }

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
            child:
                Column(children: [for (final item in data.pinned) row(item)]),
          ),
        if (data.pinned.isNotEmpty && data.recent.isNotEmpty)
          const SizedBox(height: 5),
        for (final item in data.recent) row(item),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    // Web hides the block when the file panel is open (console mount,
    // recents.js:137-143,164-169) or when there are no rows to render
    // (recents.js:164-169) — header included, in both cases.
    final visible = _visibleData;
    if ((widget.hideWhenFiles && widget.isFilesPanelOpen) ||
        visible == null ||
        (visible.pinned.isEmpty && visible.recent.isEmpty)) {
      return const SizedBox.shrink();
    }
    final active = widget.activePaneRef;
    final hasActive = active != null && active.isNotEmpty;
    final activePinned =
        hasActive && visible.pinned.any((item) => item.paneRef == active);
    // Web head is just `最近` (recents.js:537-553 markup): no count.
    // Header tools (recents.js:545-552): fold, pin-selected (disabled
    // with no active pane), clear (deletes all unpinned rows).
    final header = Row(
      children: [
        IconButton(
          key: const Key('recent-fold'),
          tooltip: _collapsed ? '展开最近' : '折叠最近',
          iconSize: 18,
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 26, minHeight: 26),
          icon: Icon(
              _collapsed ? Icons.keyboard_arrow_up : Icons.keyboard_arrow_down,
              size: 18,
              color: AppTheme.faint),
          onPressed: _toggleCollapsed,
        ),
        Expanded(
          child: Text('最近',
              style: TextStyle(
                  fontSize: 10.5, letterSpacing: 0.06, color: AppTheme.faint)),
        ),
        IconButton(
          key: const Key('recent-pin-selected'),
          tooltip: activePinned ? '取消固定当前会话' : '固定当前会话',
          iconSize: 18,
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 26, minHeight: 26),
          icon: Icon(activePinned ? Icons.push_pin : Icons.push_pin_outlined,
              size: 18, color: activePinned ? AppTheme.accent : AppTheme.faint),
          onPressed: hasActive
              ? () =>
                  _afterAction(() => widget.onTogglePin(active, !activePinned))
              : null,
        ),
        IconButton(
          key: const Key('recent-clear'),
          tooltip: '清空最近记录（固定的不动）',
          iconSize: 18,
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 26, minHeight: 26),
          icon: Icon(Icons.close, size: 18, color: AppTheme.faint),
          onPressed: widget.onClearUnpinned == null
              ? null
              : () => _afterAction(() => widget.onClearUnpinned!(
                  [for (final item in visible.recent) item.paneRef])),
        ),
      ],
    );
    if (!widget.fixedBottom) {
      return Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          header,
          if (!_collapsed)
            (widget.fillHeight ? Expanded(child: _rows()) : _rows()),
        ],
      );
    }
    if (_collapsed) {
      return Container(
        key: const Key('recent-block'),
        padding: const EdgeInsets.fromLTRB(6, 6, 6, 4),
        decoration: BoxDecoration(
            border: Border(top: BorderSide(color: AppTheme.line))),
        child: header,
      );
    }
    return Container(
      key: const Key('recent-block'),
      height: _heightLoaded
          ? _heightPx
          : RecentBlock.heightRatioDefault * _totalHeight,
      padding: const EdgeInsets.fromLTRB(6, 6, 6, 4),
      decoration:
          BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // Draggable-height grip (recents.css .recent-grip): drag up
          // to grow the block, down to shrink it. Web persists a
          // RATIO of the sidebar height (recents.js --recent-h);
          // double-click resets to 0.4 and clears the key.
          GestureDetector(
            key: const Key('recent-grip'),
            onVerticalDragUpdate: (details) {
              setState(() {
                _ratio = ((_heightPx - details.delta.dy) / _totalHeight).clamp(
                    RecentBlock.heightRatioMin, RecentBlock.heightRatioMax);
              });
            },
            onVerticalDragEnd: (_) async {
              try {
                final prefs = await SharedPreferences.getInstance();
                await prefs.setDouble(RecentBlock.heightKey, _ratio);
              } catch (_) {}
            },
            onDoubleTap: () async {
              setState(() => _ratio = RecentBlock.heightRatioDefault);
              try {
                final prefs = await SharedPreferences.getInstance();
                await prefs.remove(RecentBlock.heightKey);
              } catch (_) {}
            },
            child: Container(
              height: 10,
              color: Colors.transparent,
              alignment: Alignment.center,
              child: Container(
                width: 28,
                height: 2,
                decoration: BoxDecoration(
                    color: AppTheme.faint,
                    borderRadius: BorderRadius.circular(2)),
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
