import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../models/grid_state.dart';
import '../models/models.dart';
import '../theme.dart';
import '../widgets/grid_pager.dart';
import '../widgets/recent_block.dart';
import 'chat_screen.dart';
import 'hosts_screen.dart';
import '../widgets/plugin_buttons.dart';
import 'terminal_screen.dart';

const _amber = Color(0xFFD89A3C);

/// Narrow breakpoint, web's own (grid.html:174,218): at or below
/// 760px the grid forces one column for every layout.
const _narrowWidth = 760.0;

/// Pager position key, web's tmux-grid-pager-pos (grid.html:997).
const _pagerPosKey = 'tmux-grid-pager-pos';

/// Multi-window page, mirroring web/grid.html: several panes side by
/// side (strip), in a grid, or free-positioned on a canvas, each cell
/// a live chat or terminal, with per-cell mode switching, maximize,
/// drag reorder, and named presets. State lives in SharedPreferences
/// under the same keys web uses, so settings export/import moves it
/// verbatim.
class GridScreen extends StatefulWidget {
  const GridScreen({super.key, required this.api, required this.baseUrl});

  final Api api;
  final String baseUrl;

  @override
  State<GridScreen> createState() => _GridScreenState();
}

class _GridScreenState extends State<GridScreen> {
  GridState _grid = GridState();
  Map<String, Map<String, dynamic>> _presets = {};
  Map<String, Pane> _panes = {};
  Map<String, String> _statuses = {};
  Map<String, String> _pinLabels = {};
  Map<String, String> _favoriteTitles = {};
  SharedPreferences? _prefs;
  Timer? _statusTimer;
  String? _lastSavedJson;

  /// Single-level canvas undo snapshot (web's layoutUndo).
  Map<String, dynamic>? _undoSnapshot;

  /// Canvas drag state (web's cdrag, grid.html:1549): the drag only
  /// becomes real after a 4px dead zone, and pointerup/pointercancel
  /// share one finish routine.
  String? _canvasDragRef;
  Offset _canvasDragStart = Offset.zero;
  CanvasRect? _canvasDragRect;
  bool _canvasDragActive = false;
  bool _canvasDragging = false;

  /// Canvas resize direction (web grid.html:411 HANDLES, :1555-1630
  /// cdrag resize branch): '' = title-bar move, otherwise one of
  /// n/s/e/w/ne/nw/se/sw. Snap guides (SNAP=6, GUIDE_DELAY=140ms,
  /// grid.html:400,447-462 queueGuides) are shared by move and
  /// resize: lines only paint after 140ms at the same position, so
  /// a fast drag never flashes them.
  String _canvasDragDir = '';
  List<({String axis, double pos})> _canvasGuides = [];
  List<({String axis, double pos})> _canvasPendingGuides = [];
  Timer? _canvasGuideTimer;
  String _canvasGuideKey = '';

  final GlobalKey<ScaffoldState> _scaffoldKey = GlobalKey<ScaffoldState>();
  final ScrollController _narrowController = ScrollController();
  int _narrowIndex = 0;
  double _narrowStep = 1;
  Offset? _pagerPos;
  bool _canvasFillScheduled = false;
  Size _lastCanvasSize = const Size(1200, 900);

  /// Keyed cell bodies (web's cells map, grid.html:696-730): the same
  /// GlobalKey per pane+mode across strip/grid/canvas, so a layout
  /// switch reparents the live Chat/Terminal state instead of
  /// remounting it (a terminal socket closes on dispose).
  final Map<String, GlobalKey> _bodyKeys = {};

  @override
  void initState() {
    super.initState();
    _narrowController.addListener(_onNarrowScroll);
    _init();
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    _canvasGuideTimer?.cancel();
    _narrowController.dispose();
    super.dispose();
  }

  void _onNarrowScroll() {
    if (!_narrowController.hasClients || _narrowStep <= 0) return;
    final index = (_narrowController.offset / _narrowStep).round().clamp(0, _grid.panes.length - 1);
    if (index != _narrowIndex) setState(() => _narrowIndex = index);
  }

  Future<void> _init() async {
    final prefs = await SharedPreferences.getInstance();
    _prefs = prefs;
    GridState restored = GridState();
    try {
      final saved = prefs.getString(GridState.gridKey);
      if (saved != null && saved.isNotEmpty) {
        final decoded = jsonDecode(saved);
        if (decoded is Map) restored = GridState.fromJson(Map<String, dynamic>.from(decoded));
      }
    } catch (_) {
      restored = GridState();
    }
    Map<String, Map<String, dynamic>> presets = {};
    try {
      final saved = prefs.getString(GridState.presetsKey);
      if (saved != null && saved.isNotEmpty) {
        final decoded = jsonDecode(saved);
        if (decoded is Map) {
          presets = decoded.map((key, value) => MapEntry(
              '$key', value is Map ? Map<String, dynamic>.from(value) : <String, dynamic>{}));
        }
      }
    } catch (_) {
      presets = {};
    }
    Offset? pagerPos;
    try {
      final saved = prefs.getString(_pagerPosKey);
      if (saved != null && saved.isNotEmpty) {
        final decoded = jsonDecode(saved);
        if (decoded is Map) {
          final left = double.tryParse('${decoded['left']}');
          final top = double.tryParse('${decoded['top']}');
          if (left != null && top != null) pagerPos = Offset(left, top);
        }
      }
    } catch (_) {
      pagerPos = null;
    }
    if (mounted) {
      setState(() {
        _grid = restored;
        _presets = presets;
        _pagerPos = pagerPos;
      });
    }
    // Canonical saved form: a later save only writes when the
    // encoded state actually differs, so loading hosts or statuses
    // never rewrites (and never clobbers) a web-shaped document.
    _lastSavedJson = jsonEncode(restored.toJson());
    // 先装定时器再 await：等主机列表回来才创建的话，用户中途退出时 dispose 已经跑过，
    // 这个 10 秒轮询会跟着进程活到天荒地老。
    _statusTimer = Timer.periodic(const Duration(seconds: 10), (_) => _loadStatuses());
    await _loadHosts();
    await _loadStatuses();
    await _loadNames();
  }

  Future<void> _loadHosts() async {
    try {
      final hosts = await widget.api.hosts();
      if (!mounted) return;
      final byRef = <String, Pane>{};
      for (final host in hosts) {
        for (final pane in host.panes) {
          byRef[pane.ref] = pane;
        }
      }
      setState(() {
        _panes = byRef;
        _grid.applyDefaultModes({for (final entry in byRef.entries) entry.key: entry.value.hasTranscript});
      });
      await _saveIfChanged();
    } catch (_) {
      // Titles stay as pane refs when hosts cannot load; the cells
      // themselves still work, like web with an empty titles map.
    }
  }

  Future<void> _loadStatuses() async {
    try {
      final statuses = await widget.api.paneStates();
      if (!mounted) return;
      var changed = statuses.length != _statuses.length;
      if (!changed) {
        for (final entry in statuses.entries) {
          if (_statuses[entry.key] != entry.value) {
            changed = true;
            break;
          }
        }
      }
      if (changed) {
        setState(() => _statuses = statuses);
      }
    } catch (_) {
      // Status dots are best-effort; a failed sweep leaves the old ones.
    }
  }

  /// Web's loadNames (grid.html:1199): recent (limit 60 on web; the
  /// Flutter Api exposes pins() with no limit parameter, so the whole
  /// recent list is read) supplies pin labels, favorites supply
  /// favorite titles. Parsed tolerantly: the server/fake shapes vary
  /// between {pane,label} maps and bare pane-id strings.
  Future<void> _loadNames() async {
    final pinLabels = <String, String>{};
    final favoriteTitles = <String, String>{};
    try {
      final data = await widget.api.pins();
      for (final key in ['recent', 'pinned']) {
        final list = data[key];
        if (list is List) {
          for (final item in list) {
            if (item is Map) {
              final pane = '${item['pane'] ?? item['id'] ?? ''}';
              final label = '${item['label'] ?? item['title'] ?? ''}'.trim();
              if (pane.isNotEmpty && label.isNotEmpty) pinLabels[pane] = label;
            }
          }
        }
      }
    } catch (_) {}
    try {
      final data = await widget.api.homeFavorites();
      final list = data['favorites'];
      if (list is List) {
        for (final item in list) {
          if (item is Map) {
            final pane = '${item['pane'] ?? item['id'] ?? ''}';
            final title = '${item['title'] ?? ''}'.trim();
            if (pane.isNotEmpty && title.isNotEmpty) favoriteTitles[pane] = title;
          }
        }
      }
    } catch (_) {}
    if (!mounted) return;
    if (pinLabels.toString() != _pinLabels.toString() || favoriteTitles.toString() != _favoriteTitles.toString()) {
      setState(() {
        _pinLabels = pinLabels;
        _favoriteTitles = favoriteTitles;
      });
    }
  }

  /// The drawer recent block's data, mirroring the hosts sidebar
  /// mount: pinned rows first, then recent rows, from GET
  /// /api/home/recent. A row tap adds the pane to the grid (web
  /// grid.html:303 side-foot hint: 点会话即加入窗口).
  Future<RecentBlockData> _loadRecentBlock() async {
    final data = await widget.api.pins();
    List<Map<String, dynamic>> rawRows(Object? value) =>
        value is List ? value.whereType<Map>().map((item) => Map<String, dynamic>.from(item)).toList() : const [];
    final pinnedRaw = rawRows(data['pinned']);
    final pinLabels = <String, String>{};
    for (final row in pinnedRaw) {
      final ref = '${row['pane'] ?? ''}';
      final label = '${row['label'] ?? ''}';
      if (ref.isNotEmpty && label.isNotEmpty) pinLabels[ref] = label;
    }
    RecentRowData toRow(Map<String, dynamic> row, bool pinned) {
      final ref = '${row['pane'] ?? ''}';
      final rawTitle = '${row['title'] ?? ''}';
      final number = (row['number'] as num?)?.toInt();
      final visited = (row['visited_at'] as num?)?.toDouble() ?? 0;
      return RecentRowData(
        paneRef: ref,
        label: nameForPane(
            paneRef: ref,
            pinLabels: pinLabels,
            favoriteTitles: _favoriteTitles,
            fallback: rawTitle.isNotEmpty ? rawTitle : ref),
        pinLabel: pinned ? '${row['label'] ?? ''}' : '',
        numberLabel: number == null ? '' : '#$number',
        status: _statuses[ref] ?? '',
        visitedLabel: pinned ? '' : formatRecentVisited(visited),
        pinned: pinned,
        starred: _favoriteTitles.containsKey(ref),
      );
    }

    return RecentBlockData(
      pinned: [for (final row in pinnedRaw) if ('${row['pane'] ?? ''}'.isNotEmpty) toRow(row, true)],
      recent: [for (final row in rawRows(data['recent'])) if ('${row['pane'] ?? ''}'.isNotEmpty) toRow(row, false)],
    );
  }

  /// Grid sidebar drawer: the shared RecentBlock, mounted like the
  /// hosts sidebar mount (not a competing sidebar). The header
  /// carries web's console entry exactly as of commit e353ff8,
  /// which unified grid with home: a chat bubble (web #i-chat)
  /// titled 聊天（控制台）, never the old #i-console terminal glyph.
  Widget _gridDrawer() {
    return Drawer(
      key: const Key('grid-drawer'),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 8, 8),
            child: Row(
              children: [
                const Expanded(
                  child: Text('会话', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
                ),
                IconButton(
                  key: const Key('grid-drawer-console'),
                  tooltip: '聊天（控制台）',
                  iconSize: 18,
                  visualDensity: VisualDensity.compact,
                  onPressed: () {
                    // Close the drawer through the Scaffold: a bare
                    // Navigator pop here would pop the grid page
                    // route itself, not the drawer.
                    _scaffoldKey.currentState?.closeDrawer();
                    Navigator.of(context).push(MaterialPageRoute(
                      builder: (_) => HostsScreen(
                          api: widget.api, baseUrl: widget.baseUrl),
                    ));
                  },
                  icon: const Icon(Icons.chat_bubble_outline, size: 18),
                ),
              ],
            ),
          ),
          Expanded(
            child: RecentBlock(
              fixedBottom: false,
              fillHeight: true,
              load: _loadRecentBlock,
              onOpen: (ref) {
                _mutate(() => _grid.add(ref));
                _scaffoldKey.currentState?.closeDrawer();
              },
              onTogglePin: (ref, pinned) async {
                await widget.api.pinPane(ref, pinned: pinned);
                await _loadNames();
              },
              onRenamePin: (ref, label) async {
                await widget.api.renamePin(ref, label);
                await _loadNames();
              },
              onToggleFavorite: (ref, starred) async {
                if (starred) {
                  await widget.api.addHomeFavorite(ref);
                } else {
                  await widget.api.deleteHomeFavorite(ref);
                }
                await _loadNames();
              },
            ),
          ),
          Container(
            padding: EdgeInsets.fromLTRB(16, 10, 16, 10 + MediaQuery.of(context).padding.bottom),
            decoration: BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
            child: Text('点会话即加入窗口', style: TextStyle(fontSize: 11.5, color: AppTheme.faint)),
          ),
        ],
      ),
    );
  }

  Future<void> _saveIfChanged() async {
    final prefs = _prefs;
    if (prefs == null) return;
    final encoded = jsonEncode(_grid.toJson());
    if (encoded == _lastSavedJson) return;
    _lastSavedJson = encoded;
    await prefs.setString(GridState.gridKey, encoded);
  }

  Future<void> _savePresets() async {
    await _prefs?.setString(GridState.presetsKey, jsonEncode(_presets));
  }

  String _titleFor(String ref) {
    final pane = _panes[ref];
    final String fallback;
    if (pane == null) {
      fallback = ref;
    } else {
      final base = pane.session.isNotEmpty ? pane.session : ref;
      final window = pane.windowName.isNotEmpty ? ':${pane.windowName}' : '';
      final command = pane.command.isNotEmpty ? ' · ${pane.command}' : '';
      final title = '$base$window$command';
      fallback = title.isNotEmpty ? title : pane.label;
    }
    final name = nameForPane(paneRef: ref, pinLabels: _pinLabels, favoriteTitles: _favoriteTitles, fallback: fallback);
    final prefix = pane?.numberLabel ?? '';
    return prefix.isNotEmpty ? '$prefix · $name' : name;
  }

  /// Web's statusText, verbatim (grid.html:1229-1234, fed by
  /// loadStatus at :1239-1250 which rebuilds the map from the
  /// sweep's `panes` list on every poll):
  ///   function statusText(pane){
  ///     if(state.liveWaiting&&state.liveWaiting.get(pane))return '思考中';
  ///     const status=state.statuses.get(pane);
  ///     if(status==='busy')return '思考中';
  ///     if(status==='offline')return '离线';
  ///     return '';
  ///   }
  /// A pane missing from the sweep yields `undefined` from the
  /// Map, so it falls through to '' — missing is NEVER offline
  /// in web, empty sweep or not. The earlier Flutter guard
  /// (missing from a non-empty sweep => offline) was a deliberate
  /// deviation and is reverted here to the literal rule.
  String _statusText(String ref) {
    if (_statuses[ref] == 'busy') return '思考中';
    if (_statuses[ref] == 'offline') return '离线';
    return '';
  }

  Color _dotColor(String ref) {
    switch (_statuses[ref]) {
      case 'idle':
        return AppTheme.green;
      case 'busy':
        return _amber;
      default:
        return AppTheme.faint;
    }
  }

  void _mutate(void Function() change) {
    setState(change);
    unawaited(_saveIfChanged());
  }

  void _toggleMax(String ref) {
    setState(() => _grid.maximized = _grid.maximized == ref ? null : ref);
  }

  void _pushUndo() {
    _undoSnapshot = _grid.layoutSnapshot();
  }

  void _undo() {
    final snapshot = _undoSnapshot;
    if (snapshot == null) return;
    setState(() {
      _grid.restoreLayoutSnapshot(snapshot);
      _undoSnapshot = null;
    });
    unawaited(_saveIfChanged());
  }

  bool _focusInTextInput() {
    final focus = FocusManager.instance.primaryFocus;
    if (focus == null) return false;
    final context = focus.context;
    if (context == null) return false;
    if (context.findAncestorWidgetOfExactType<EditableText>() != null) return true;
    return context.findAncestorStateOfType<EditableTextState>() != null;
  }

  KeyEventResult _onKeyEvent(FocusNode node, KeyEvent event) {
    if (event is! KeyDownEvent) return KeyEventResult.ignored;
    final keyboard = HardwareKeyboard.instance;
    if (!(keyboard.isControlPressed || keyboard.isMetaPressed) || keyboard.isShiftPressed) {
      return KeyEventResult.ignored;
    }
    if (event.logicalKey != LogicalKeyboardKey.keyZ) return KeyEventResult.ignored;
    // Web grid.html:1417: Ctrl/Cmd+Z only in canvas mode, skipped
    // when focus is in a text input.
    if (!_grid.isCanvas || _undoSnapshot == null || _focusInTextInput()) {
      return KeyEventResult.ignored;
    }
    _undo();
    return KeyEventResult.handled;
  }

  Widget _cellBody(String ref) {
    final pane = _panes[ref] ?? Pane(ref: ref, title: ref, command: '');
    final mode = _grid.modeFor(ref);
    final key = _bodyKeys.putIfAbsent('$ref|$mode', () => GlobalKey());
    if (mode == 'term') {
      final socketApi = widget.api is TerminalApi ? widget.api as TerminalApi : null;
      if (socketApi == null) return const Center(child: Text('终端需要 socket 支持'));
      return TerminalScreen(key: key, api: socketApi, pane: pane, embedded: true);
    }
    return ChatScreen(
      key: key,
      source: PaneChatSource(api: widget.api, pane: pane),
      terminalApi: widget.api is TerminalApi ? widget.api as TerminalApi : null,
      embedded: true,
      baseUrl: widget.baseUrl,
    );
  }

  Widget _cellHeader(String ref, {Widget Function(Widget title)? titleWrapper}) {
    final mode = _grid.modeFor(ref);
    final statusText = _statusText(ref);
    final wrap = titleWrapper ?? (Widget title) => title;
    return Container(
      height: 34,
      padding: const EdgeInsets.only(left: 10, right: 6),
      decoration: BoxDecoration(
        color: AppTheme.page,
        border: Border(bottom: BorderSide(color: AppTheme.line)),
      ),
      child: Row(
        children: [
          if (_statuses[ref] == 'busy')
            const _BusyDot()
          else
            Container(
              width: 7,
              height: 7,
              decoration: BoxDecoration(shape: BoxShape.circle, color: _dotColor(ref)),
            ),
          const SizedBox(width: 7),
          Expanded(
            child: GestureDetector(
              // Web grid.html:1371: double-click on the cell header
              // maximizes. Only the title area carries the recognizer,
              // so header button taps are never delayed by the
              // double-tap timeout (the old explicit button is gone:
              // web has no header maximize button).
              onDoubleTap: () => _toggleMax(ref),
              child: wrap(
                Text(_titleFor(ref),
                    maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w600)),
              ),
            ),
          ),
          if (statusText.isNotEmpty) ...[
            const SizedBox(width: 6),
            Container(
              key: Key('grid-state-$ref'),
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
              decoration: BoxDecoration(
                color: AppTheme.accent.withValues(alpha: 0.12),
                borderRadius: BorderRadius.circular(999),
              ),
              child: Text(statusText, style: const TextStyle(fontSize: 10.5)),
            ),
          ],
          IconButton(
            key: Key('grid-mode-$ref'),
            tooltip: mode == 'term' ? '切到聊天' : '切到终端',
            iconSize: 16,
            visualDensity: VisualDensity.compact,
            onPressed: () => _mutate(() => _grid.setMode(ref, mode == 'term' ? 'chat' : 'term')),
            icon: Icon(mode == 'term' ? Icons.chat_bubble_outline : Icons.terminal, size: 16),
          ),
          IconButton(
            key: Key('grid-open-$ref'),
            tooltip: '在控制台打开',
            iconSize: 16,
            visualDensity: VisualDensity.compact,
            // Web grid.html:714 (commit ffed594): the open-in-console
            // glyph is a folded-corner page, NOT Icons.open_in_new
            // (the superseded eb45c18 arrow). It opens the console
            // (hosts/detail view), never a full ChatScreen.
            onPressed: () {
              Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => HostsScreen(
                    api: widget.api,
                    baseUrl: widget.baseUrl,
                    initialPaneRef: ref),
              ));
            },
            icon: const _PageGlyph(),
          ),
          IconButton(
            key: Key('grid-remove-$ref'),
            tooltip: '移出多窗口',
            iconSize: 16,
            visualDensity: VisualDensity.compact,
            onPressed: () => _mutate(() => _grid.remove(ref)),
            icon: const Icon(Icons.close, size: 16),
          ),
        ],
      ),
    );
  }

  Widget _draggableCell(String ref, {double? width}) {
    final cell = Container(
      decoration: BoxDecoration(
        color: AppTheme.card,
        border: Border.all(color: AppTheme.line),
        borderRadius: BorderRadius.circular(12),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        children: [
          LongPressDraggable<String>(
            data: ref,
            feedback: Material(
              elevation: 4,
              borderRadius: BorderRadius.circular(8),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                child: Text(_titleFor(ref), style: const TextStyle(fontSize: 13)),
              ),
            ),
            childWhenDragging: Opacity(opacity: 0.5, child: _cellHeader(ref)),
            child: _cellHeader(ref),
          ),
          Expanded(child: _cellBody(ref)),
        ],
      ),
    );
    final target = DragTarget<String>(
      onWillAcceptWithDetails: (details) => details.data != ref,
      onAcceptWithDetails: (details) => _mutate(() => _grid.movePane(details.data, ref)),
      builder: (context, candidate, rejected) => Container(
        decoration: candidate.isNotEmpty
            ? BoxDecoration(border: Border.all(color: AppTheme.accent, width: 2), borderRadius: BorderRadius.circular(12))
            : null,
        child: cell,
      ),
    );
    if (width != null) return SizedBox(width: width, child: target);
    return target;
  }

  // ---- canvas ----

  CanvasRect _canvasRectFor(String ref, double areaWidth, double areaHeight) {
    final stored = _grid.cells[ref];
    if (stored != null) return stored;
    final cols = _grid.canvasCols > 0 ? _grid.canvasCols : _grid.cols;
    final rows = _grid.canvasRows > 0 ? _grid.canvasRows : (_grid.panes.length / cols).ceil().clamp(1, 1 << 30);
    return GridState.slotRect(_grid.panes.indexOf(ref), cols, rows, areaWidth, areaHeight);
  }

  void _canvasPointerDown(String ref, PointerDownEvent event, {String dir = ''}) {
    if (_grid.maximized != null) return;
    _canvasDragRef = ref;
    _canvasDragStart = event.position;
    _canvasDragRect = _canvasRectFor(ref, _lastCanvasSize.width, _lastCanvasSize.height);
    _canvasDragActive = false;
    _canvasDragDir = dir;
  }

  /// Web's snapTargets (grid.html:464-474): canvas edges (8px inset)
  /// plus every other cell's left/center/right and top/middle/bottom.
  ({List<double> xs, List<double> ys}) _canvasSnapTargets(String ignore, double areaWidth, double areaHeight) {
    final xs = <double>[8, math.max(8, areaWidth - 8)];
    final ys = <double>[8, math.max(8, areaHeight - 8)];
    for (final other in _grid.panes) {
      if (other == ignore) continue;
      final rect = _canvasRectFor(other, areaWidth, areaHeight);
      xs.addAll([rect.x, rect.x + rect.w, rect.x + rect.w / 2]);
      ys.addAll([rect.y, rect.y + rect.h, rect.y + rect.h / 2]);
    }
    return (xs: xs, ys: ys);
  }

  /// Web's nearest (grid.html:476-483): closest target within SNAP=6,
  /// else null.
  double? _canvasNearest(double value, List<double> targets) {
    double? best;
    for (final target in targets) {
      if ((value - target).abs() > 6) continue;
      if (best == null || (value - target).abs() < (value - best).abs()) best = target;
    }
    return best;
  }

  /// Web's queueGuides (grid.html:447-462): a changed line set waits
  /// GUIDE_DELAY=140ms before painting; an empty set hides at once.
  void _queueCanvasGuides(List<({String axis, double pos})> lines) {
    final key = lines.map((line) => '${line.axis}${line.pos.round()}').join(',');
    if (key == _canvasGuideKey) return;
    _canvasGuideKey = key;
    _canvasGuideTimer?.cancel();
    _canvasGuideTimer = null;
    if (lines.isEmpty) {
      if (_canvasGuides.isNotEmpty) setState(() => _canvasGuides = []);
      return;
    }
    _canvasPendingGuides = lines;
    _canvasGuideTimer = Timer(const Duration(milliseconds: 140), () {
      _canvasGuideTimer = null;
      if (!mounted) return;
      setState(() => _canvasGuides = _canvasPendingGuides);
    });
  }

  void _canvasPointerMove(String ref, PointerMoveEvent event) {
    if (_canvasDragRef != ref || _canvasDragRect == null) return;
    final delta = event.position - _canvasDragStart;
    if (!_canvasDragActive) {
      // 4px 之内当点击；超过才进入拖动，并在这时才锁住画布滚动/iframe
      // 输入 (web grid.html:1577, 78d85bb finish routine).
      if (delta.distance < 4) return;
      _canvasDragActive = true;
      _pushUndo();
      setState(() => _canvasDragging = true);
    }
    final start = _canvasDragRect!;
    final targets = _canvasSnapTargets(ref, _lastCanvasSize.width, _lastCanvasSize.height);
    final lines = <({String axis, double pos})>[];
    late final CanvasRect next;
    if (_canvasDragDir.isEmpty) {
      // Web move branch (grid.html:1585-1595): left beats center
      // beats right, top beats middle beats bottom.
      var x = (start.x + delta.dx).clamp(0.0, double.infinity);
      var y = (start.y + delta.dy).clamp(0.0, double.infinity);
      final byLeft = _canvasNearest(x, targets.xs);
      final byCenter = _canvasNearest(x + start.w / 2, targets.xs);
      final byRight = _canvasNearest(x + start.w, targets.xs);
      if (byLeft != null) {
        x = byLeft;
        lines.add((axis: 'x', pos: byLeft));
      } else if (byCenter != null) {
        x = byCenter - start.w / 2;
        lines.add((axis: 'x', pos: byCenter));
      } else if (byRight != null) {
        x = byRight - start.w;
        lines.add((axis: 'x', pos: byRight));
      }
      final byTop = _canvasNearest(y, targets.ys);
      final byMiddle = _canvasNearest(y + start.h / 2, targets.ys);
      final byBottom = _canvasNearest(y + start.h, targets.ys);
      if (byTop != null) {
        y = byTop;
        lines.add((axis: 'y', pos: byTop));
      } else if (byMiddle != null) {
        y = byMiddle - start.h / 2;
        lines.add((axis: 'y', pos: byMiddle));
      } else if (byBottom != null) {
        y = byBottom - start.h;
        lines.add((axis: 'y', pos: byBottom));
      }
      next = CanvasRect(x: x, y: y, w: start.w, h: start.h);
    } else {
      // Web resize branch (grid.html:1596-1625): each edge snaps
      // its moving side, then the min size clamps it. All edges
      // measure against the pre-drag rect, like web's cdrag.rect.
      final dir = _canvasDragDir;
      var x = start.x;
      var y = start.y;
      var w = start.w;
      var h = start.h;
      if (dir.contains('e')) {
        var right = start.x + start.w + delta.dx;
        final snap = _canvasNearest(right, targets.xs);
        if (snap != null) {
          right = snap;
          lines.add((axis: 'x', pos: snap));
        }
        w = math.max(GridState.canvasMinWidth, right - start.x);
      }
      if (dir.contains('s')) {
        var bottom = start.y + start.h + delta.dy;
        final snap = _canvasNearest(bottom, targets.ys);
        if (snap != null) {
          bottom = snap;
          lines.add((axis: 'y', pos: snap));
        }
        h = math.max(GridState.canvasMinHeight, bottom - start.y);
      }
      if (dir.contains('w')) {
        var left = start.x + delta.dx;
        final snap = _canvasNearest(left, targets.xs);
        if (snap != null) {
          left = snap;
          lines.add((axis: 'x', pos: snap));
        }
        left = math.min(left, start.x + start.w - GridState.canvasMinWidth);
        w = math.max(GridState.canvasMinWidth, start.x + start.w - left);
        x = math.max(0, left);
      }
      if (dir.contains('n')) {
        var top = start.y + delta.dy;
        final snap = _canvasNearest(top, targets.ys);
        if (snap != null) {
          top = snap;
          lines.add((axis: 'y', pos: snap));
        }
        top = math.min(top, start.y + start.h - GridState.canvasMinHeight);
        h = math.max(GridState.canvasMinHeight, start.y + start.h - top);
        y = math.max(0, top);
      }
      next = CanvasRect(x: x, y: y, w: w, h: h);
    }
    setState(() => _grid.cells[ref] = next);
    _queueCanvasGuides(lines);
  }

  void _finishCanvasDrag() {
    final ref = _canvasDragRef;
    if (ref == null) return;
    final active = _canvasDragActive;
    _canvasDragRef = null;
    _canvasDragRect = null;
    _canvasDragActive = false;
    _canvasDragDir = '';
    _canvasGuideTimer?.cancel();
    _canvasGuideTimer = null;
    _canvasGuideKey = '';
    if (_canvasDragging) setState(() => _canvasDragging = false);
    if (_canvasGuides.isNotEmpty) setState(() => _canvasGuides = []);
    if (!active) return; // 只是点了一下，窗口不该动，也不用保存
    final rect = _grid.cells[ref];
    if (rect != null) _grid.cells[ref] = rect.rounded();
    unawaited(_saveIfChanged());
    if (mounted) setState(() {});
  }

  /// One canvas resize handle (web grid.html:411-425 attachHandles
  /// + :124-132 positions): 8 directions, edge strips 8px thick
  /// inset 10px from the sides, corners 14px square overhanging by
  /// 4px. Handles sit above the clipped card (Stack clip none) so
  /// the overhang stays hittable, and win the hit test over the
  /// header beneath them, like web's handle-first pointerdown.
  Widget _canvasHandle(String ref, String dir) {
    final isCorner = dir.length == 2;
    final cursor = switch (dir) {
      'n' || 's' => SystemMouseCursors.resizeUpDown,
      'e' || 'w' => SystemMouseCursors.resizeLeftRight,
      'nw' || 'se' => SystemMouseCursors.resizeUpLeftDownRight,
      _ => SystemMouseCursors.resizeUpRightDownLeft,
    };
    double? top, bottom, left, right, width, height;
    if (isCorner) {
      width = 14;
      height = 14;
      if (dir.contains('n')) top = -4;
      if (dir.contains('s')) bottom = -4;
      if (dir.contains('w')) left = -4;
      if (dir.contains('e')) right = -4;
    } else if (dir == 'n') {
      top = -3;
      left = 10;
      right = 10;
      height = 8;
    } else if (dir == 's') {
      bottom = -3;
      left = 10;
      right = 10;
      height = 8;
    } else if (dir == 'w') {
      left = -3;
      top = 10;
      bottom = 10;
      width = 8;
    } else {
      right = -3;
      top = 10;
      bottom = 10;
      width = 8;
    }
    return Positioned(
      top: top,
      bottom: bottom,
      left: left,
      right: right,
      width: width,
      height: height,
      child: MouseRegion(
        cursor: cursor,
        child: Listener(
          behavior: HitTestBehavior.opaque,
          onPointerDown: (event) => _canvasPointerDown(ref, event, dir: dir),
          onPointerMove: (event) => _canvasPointerMove(ref, event),
          onPointerUp: (_) => _finishCanvasDrag(),
          onPointerCancel: (_) => _finishCanvasDrag(),
          child: Container(key: Key('canvas-handle-$ref-$dir'), color: Colors.transparent),
        ),
      ),
    );
  }

  Widget _canvasCell(String ref) {
    final card = Container(
      decoration: BoxDecoration(
        color: AppTheme.card,
        border: Border.all(color: AppTheme.line),
        borderRadius: BorderRadius.circular(12),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        children: [
          _cellHeader(ref, titleWrapper: (title) {
            // Only the title area drags the cell (web excludes the
            // header buttons); Listener observes raw pointers so the
            // 4px dead zone is exact, and pointerup AND pointercancel
            // both finalize through _finishCanvasDrag.
            return Listener(
              behavior: HitTestBehavior.opaque,
              onPointerDown: (event) => _canvasPointerDown(ref, event),
              onPointerMove: (event) => _canvasPointerMove(ref, event),
              onPointerUp: (_) => _finishCanvasDrag(),
              onPointerCancel: (_) => _finishCanvasDrag(),
              child: title,
            );
          }),
          Expanded(
            child: IgnorePointer(
              // Web body.canvas-dragging: iframe input locked while dragging.
              ignoring: _canvasDragging,
              child: _cellBody(ref),
            ),
          ),
        ],
      ),
    );
    return Stack(
      clipBehavior: Clip.none,
      fit: StackFit.expand,
      children: [
        card,
        for (final dir in const ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']) _canvasHandle(ref, dir),
      ],
    );
  }

  Widget _canvasLayout(BoxConstraints constraints) {
    final panes = _grid.panes;
    if (_grid.maximized != null) {
      return Padding(
        padding: const EdgeInsets.all(8),
        child: _draggableCell(_grid.maximized!),
      );
    }
    final areaWidth = constraints.maxWidth;
    final areaHeight = constraints.maxHeight;
    _lastCanvasSize = Size(areaWidth, areaHeight);
    var extentWidth = areaWidth;
    var extentHeight = areaHeight;
    final rects = <String, CanvasRect>{};
    for (final ref in panes) {
      final rect = _canvasRectFor(ref, areaWidth, areaHeight);
      rects[ref] = rect;
      if (rect.x + rect.w + 8 > extentWidth) extentWidth = rect.x + rect.w + 8;
      if (rect.y + rect.h + 8 > extentHeight) extentHeight = rect.y + rect.h + 8;
    }
    // Panes with no stored cell yet (first canvas visit, or newly
    // added) get their slot persisted after this frame, like web's
    // ensureCanvasCells — never during build itself.
    if (panes.any((ref) => !_grid.cells.containsKey(ref)) && !_canvasFillScheduled) {
      _canvasFillScheduled = true;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _canvasFillScheduled = false;
        if (!mounted) return;
        var changed = false;
        for (var i = 0; i < _grid.panes.length; i++) {
          final ref = _grid.panes[i];
          if (!_grid.cells.containsKey(ref)) {
            _grid.cells[ref] = _canvasRectFor(ref, _lastCanvasSize.width, _lastCanvasSize.height);
            changed = true;
          }
        }
        if (changed) {
          setState(() {});
          unawaited(_saveIfChanged());
        }
      });
    }
    final physics = _canvasDragging ? const NeverScrollableScrollPhysics() : const ClampingScrollPhysics();
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      physics: physics,
      child: SingleChildScrollView(
        physics: physics,
        child: SizedBox(
          width: extentWidth,
          height: extentHeight,
          child: Stack(
            children: [
              for (final ref in panes)
                Positioned(
                  left: rects[ref]!.x,
                  top: rects[ref]!.y,
                  width: rects[ref]!.w,
                  height: rects[ref]!.h,
                  child: _canvasCell(ref),
                ),
              // Snap guides (web .canvas-guides): full-extent lines
              // at the snapped positions, painted only after the
              // 140ms dwell in _queueCanvasGuides.
              for (final line in _canvasGuides)
                if (line.axis == 'x')
                  Positioned(
                    key: Key('canvas-guide-x-${line.pos.round()}'),
                    left: line.pos,
                    top: 0,
                    bottom: 0,
                    child: Container(width: 1, color: AppTheme.accent),
                  )
                else
                  Positioned(
                    key: Key('canvas-guide-y-${line.pos.round()}'),
                    top: line.pos,
                    left: 0,
                    right: 0,
                    child: Container(height: 1, color: AppTheme.accent),
                  ),
              // Size badge (web showBadge): W×H at the dragged
              // cell's top-left, clamped into the visible area.
              if (_canvasDragging && _canvasDragRef != null && _grid.cells[_canvasDragRef] != null)
                Positioned(
                  key: const Key('canvas-badge'),
                  left: _grid.cells[_canvasDragRef]!.x.clamp(0.0, math.max(0.0, areaWidth - 90)),
                  top: math.max(0.0, _grid.cells[_canvasDragRef]!.y - 22),
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    decoration: BoxDecoration(
                      color: AppTheme.card,
                      border: Border.all(color: AppTheme.line),
                      borderRadius: BorderRadius.circular(6),
                    ),
                    child: Text(
                      '${_grid.cells[_canvasDragRef]!.w.round()}×${_grid.cells[_canvasDragRef]!.h.round()}',
                      style: const TextStyle(fontSize: 11),
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _stripLayout() {
    final panes = _grid.panes;
    if (_grid.maximized != null) {
      return Padding(
        padding: const EdgeInsets.all(8),
        child: _draggableCell(_grid.maximized!),
      );
    }
    final children = <Widget>[];
    for (var i = 0; i < panes.length; i++) {
      final ref = panes[i];
      children.add(_draggableCell(ref, width: _grid.widths[ref] ?? 360));
      if (i < panes.length - 1) {
        children.add(GestureDetector(
          key: Key('grid-split-$i'),
          onHorizontalDragUpdate: (details) {
            final left = panes[i];
            final right = panes[i + 1];
            final leftWidth = ((_grid.widths[left] ?? 360) + details.delta.dx).clamp(GridState.minStripWidth, double.infinity);
            final rightWidth = ((_grid.widths[right] ?? 360) - details.delta.dx).clamp(GridState.minStripWidth, double.infinity);
            setState(() {
              _grid.widths[left] = leftWidth;
              _grid.widths[right] = rightWidth;
            });
          },
          onHorizontalDragEnd: (_) => _saveIfChanged(),
          child: Container(
            width: 6,
            alignment: Alignment.center,
            child: Container(width: 2, margin: const EdgeInsets.symmetric(vertical: 12), color: AppTheme.line),
          ),
        ));
      }
    }
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: children),
    );
  }

  /// Grid seam grips (web grid.html:1120-1140 positionGrips,
  /// :1469-1480 pointerdown, :1506-1525 pointermove): horizontal seams
  /// adjust colFr, vertical seams rowFr, live, persisted on release.
  /// Rendered only in grid layout, wide, not maximized. These are NOT
  /// the strip splitters (strip widths, grid_screen.dart:311-328).
  Widget _colGrip(int row, int seam, double totalWidth) {
    return GestureDetector(
      key: Key('grid-grip-col-$row-$seam'),
      behavior: HitTestBehavior.opaque,
      onHorizontalDragUpdate: (details) {
        final fr = _grid.colFr;
        if (fr.length < 2 || seam >= fr.length - 1) return;
        final sum = fr.fold<double>(0, (a, b) => a + b);
        if (sum <= 0 || totalWidth <= 0) return;
        final perPx = sum / totalWidth;
        final pair = fr[seam] + fr[seam + 1];
        final minFr = (170 * perPx).clamp(0.0, pair / 2 - 0.001);
        if (minFr >= pair - minFr) return;
        final next = (fr[seam] + details.delta.dx * perPx).clamp(minFr, pair - minFr);
        setState(() {
          fr[seam] = double.parse(next.toStringAsFixed(4));
          fr[seam + 1] = double.parse((pair - next).toStringAsFixed(4));
        });
      },
      onHorizontalDragEnd: (_) => _saveIfChanged(),
      child: MouseRegion(
        cursor: SystemMouseCursors.resizeColumn,
        child: Container(
          width: 9,
          alignment: Alignment.center,
          child: Container(width: 2, margin: const EdgeInsets.symmetric(vertical: 12), color: AppTheme.line),
        ),
      ),
    );
  }

  Widget _rowGrip(int seam, double totalHeight) {
    return GestureDetector(
      key: Key('grid-grip-row-$seam'),
      behavior: HitTestBehavior.opaque,
      onVerticalDragUpdate: (details) {
        final fr = _grid.rowFr;
        if (fr.length < 2 || seam >= fr.length - 1) return;
        final sum = fr.fold<double>(0, (a, b) => a + b);
        if (sum <= 0 || totalHeight <= 0) return;
        final perPx = sum / totalHeight;
        final pair = fr[seam] + fr[seam + 1];
        final minFr = (170 * perPx).clamp(0.0, pair / 2 - 0.001);
        if (minFr >= pair - minFr) return;
        final next = (fr[seam] + details.delta.dy * perPx).clamp(minFr, pair - minFr);
        setState(() {
          fr[seam] = double.parse(next.toStringAsFixed(4));
          fr[seam + 1] = double.parse((pair - next).toStringAsFixed(4));
        });
      },
      onVerticalDragEnd: (_) => _saveIfChanged(),
      child: MouseRegion(
        cursor: SystemMouseCursors.resizeRow,
        child: Container(
          height: 9,
          alignment: Alignment.center,
          child: Container(height: 2, margin: const EdgeInsets.symmetric(horizontal: 12), color: AppTheme.line),
        ),
      ),
    );
  }

  Widget _gridLayout(int effectiveCols, double maxWidth, double maxHeight, {required bool showGrips}) {
    final panes = _grid.panes;
    if (_grid.maximized != null) {
      return Padding(
        padding: const EdgeInsets.all(8),
        child: _draggableCell(_grid.maximized!),
      );
    }
    final rows = <List<String>>[];
    for (var i = 0; i < panes.length; i += effectiveCols) {
      rows.add(panes.sublist(i, (i + effectiveCols).clamp(0, panes.length)));
    }
    int flexOf(double value) => (value * 10).round().clamp(1, 1000);
    return Padding(
      padding: const EdgeInsets.all(8),
      child: Column(
        children: [
          for (var r = 0; r < rows.length; r++) ...[
            Expanded(
              flex: flexOf(_grid.rowFr.length > r ? _grid.rowFr[r] : 1),
              child: Padding(
                padding: EdgeInsets.only(bottom: r < rows.length - 1 ? 8 : 0),
                child: Row(
                  children: [
                    for (var c = 0; c < effectiveCols; c++) ...[
                      Expanded(
                        flex: flexOf(_grid.colFr.length > c ? _grid.colFr[c] : 1),
                        child: Padding(
                          padding: EdgeInsets.only(right: c < effectiveCols - 1 ? 8 : 0),
                          child: c < rows[r].length ? _draggableCell(rows[r][c]) : const SizedBox(),
                        ),
                      ),
                      if (showGrips && c < effectiveCols - 1) _colGrip(r, c, maxWidth - 16),
                    ],
                  ],
                ),
              ),
            ),
            if (showGrips && r < rows.length - 1) _rowGrip(r, maxHeight - 16),
          ],
        ],
      ),
    );
  }

  Widget _narrowLayout(double viewportHeight) {
    final panes = _grid.panes;
    if (_grid.maximized != null) {
      return Padding(
        padding: const EdgeInsets.all(8),
        child: _draggableCell(_grid.maximized!),
      );
    }
    _narrowStep = viewportHeight;
    final pager = panes.length >= 2
        ? GridPager(
            index: _narrowIndex.clamp(0, panes.length - 1) + 1,
            total: panes.length,
            onUp: () => _pagerTo(_narrowIndex - 1),
            onDown: () => _pagerTo(_narrowIndex + 1),
            savedLeft: _pagerPos?.dx,
            savedTop: _pagerPos?.dy,
            onPositionChanged: (left, top) async {
              setState(() => _pagerPos = Offset(left, top));
              await _prefs?.setString(_pagerPosKey, jsonEncode({'left': left, 'top': top}));
            },
            onPositionReset: () async {
              setState(() => _pagerPos = null);
              await _prefs?.remove(_pagerPosKey);
            },
          )
        : null;
    return Stack(
      children: [
        SingleChildScrollView(
          controller: _narrowController,
          physics: _canvasDragging ? const NeverScrollableScrollPhysics() : const PageScrollPhysics(),
          child: Column(
            children: [
              for (final ref in panes)
                SizedBox(
                  height: viewportHeight,
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(10, 8, 10, 0),
                    child: _draggableCell(ref),
                  ),
                ),
            ],
          ),
        ),
        if (pager != null) pager,
      ],
    );
  }

  void _pagerTo(int index) {
    final target = index.clamp(0, _grid.panes.length - 1);
    if (_narrowController.hasClients) {
      unawaited(_narrowController.animateTo(
        target * _narrowStep,
        duration: const Duration(milliseconds: 300),
        curve: Curves.easeOut,
      ));
    } else {
      setState(() => _narrowIndex = target);
    }
  }

  /// Apply a shape from the picker/presets/custom chip. In canvas
  /// mode this tiles the canvas (with an undo snapshot, like web's
  /// applyShape); in grid mode it is setGridSize (same-shape clicks
  /// keep hand-dragged fr values).
  void _applyShape(int cols, int rows, Size areaSize) {
    if (_grid.isCanvas) {
      setState(() {
        _pushUndo();
        _grid.tileCanvas(cols, rows, areaSize.width, areaSize.height);
      });
    } else {
      _mutate(() => _grid.setGridSize(cols, rows));
      return;
    }
    unawaited(_saveIfChanged());
  }

  String _shapeText() {
    if (_grid.isCanvas) {
      return _grid.canvasCols > 0 ? '${_grid.canvasCols}×${_grid.canvasRows}' : '自由';
    }
    return '${_grid.cols}×${_grid.rows}';
  }

  Future<void> _showShapePicker(Size areaSize) async {
    var hoverCols = -1;
    var hoverRows = -1;
    final currentCols = _grid.isCanvas ? (_grid.canvasCols > 0 ? _grid.canvasCols : _grid.cols) : _grid.cols;
    final currentRows = _grid.isCanvas ? (_grid.canvasRows > 0 ? _grid.canvasRows : _grid.rows) : _grid.rows;
    await showDialog(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) {
          final hintCols = hoverCols > 0 ? hoverCols : currentCols;
          final hintRows = hoverRows > 0 ? hoverRows : currentRows;
          return AlertDialog(
            title: const Text('选择行列'),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                for (var r = 1; r <= 6; r++)
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      for (var c = 1; c <= 6; c++)
                        Padding(
                          padding: const EdgeInsets.all(1.5),
                          child: SizedBox(
                            width: 22,
                            height: 22,
                            child: Material(
                              color: c <= hintCols && r <= hintRows
                                  ? AppTheme.accent.withValues(alpha: 0.16)
                                  : AppTheme.card,
                              shape: RoundedRectangleBorder(
                                borderRadius: BorderRadius.circular(4),
                                side: BorderSide(
                                  color: c == hintCols && r == hintRows ? AppTheme.accent : AppTheme.line,
                                ),
                              ),
                              child: InkWell(
                                key: Key('grid-shape-${c}x$r'),
                                onHover: (hovering) => setDialogState(() {
                                  hoverCols = hovering ? c : -1;
                                  hoverRows = hovering ? r : -1;
                                }),
                                onTap: () {
                                  Navigator.of(context).pop();
                                  _applyShape(c, r, areaSize);
                                },
                              ),
                            ),
                          ),
                        ),
                    ],
                  ),
                const SizedBox(height: 8),
                Row(
                  children: [
                    Text('$hintCols × $hintRows', style: TextStyle(color: AppTheme.muted, fontSize: 11.5)),
                    const Spacer(),
                    TextButton(
                      key: const Key('grid-shape-custom'),
                      onPressed: () {
                        Navigator.of(context).pop();
                        _showCustomShape(areaSize);
                      },
                      child: const Text('自定义…'),
                    ),
                  ],
                ),
              ],
            ),
            actions: [
              TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('关闭')),
            ],
          );
        },
      ),
    );
  }

  Future<void> _showCustomShape(Size areaSize) async {
    final colsController = TextEditingController(text: '${_grid.cols}');
    final rowsController = TextEditingController(text: '${_grid.rows}');
    await showDialog(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) {
          // Live hint, like web's updateShapeHint (grid.html:922).
          final cols = (int.tryParse(colsController.text) ?? _grid.cols).clamp(1, 6);
          final rows = (int.tryParse(rowsController.text) ?? _grid.rows).clamp(1, 6);
          final panes = _grid.panes.length;
          final need = panes == 0 ? 1 : (panes / cols).ceil();
          final parts = ['$cols 列 × $rows 行'];
          if (panes > 0) parts.add('当前 $panes 个窗口');
          if (panes > 0 && need > rows) {
            parts.add('按 $cols 列会占 $need 行（行数自动加到 ${need.clamp(1, 6)}）');
          } else if (panes > 0 && panes < cols * rows) {
            parts.add('${cols * rows - panes} 个空位');
          }
          final hint = '${parts.join('，')}。最多 6 × 6。';
          return AlertDialog(
            title: const Text('自定义网格'),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: TextField(
                        key: const Key('grid-custom-cols'),
                        controller: colsController,
                        keyboardType: TextInputType.number,
                        decoration: const InputDecoration(labelText: '列'),
                        onChanged: (_) => setDialogState(() {}),
                        onSubmitted: (_) => _applyCustomShape(cols, rows, areaSize),
                      ),
                    ),
                    const Padding(padding: EdgeInsets.symmetric(horizontal: 8), child: Text('×')),
                    Expanded(
                      child: TextField(
                        key: const Key('grid-custom-rows'),
                        controller: rowsController,
                        keyboardType: TextInputType.number,
                        decoration: const InputDecoration(labelText: '行'),
                        onChanged: (_) => setDialogState(() {}),
                        onSubmitted: (_) => _applyCustomShape(cols, rows, areaSize),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Align(
                  alignment: Alignment.centerLeft,
                  child: Text(hint, style: TextStyle(color: AppTheme.faint, fontSize: 11.5)),
                ),
              ],
            ),
            actions: [
              TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('取消')),
              FilledButton(
                key: const Key('grid-custom-apply'),
                onPressed: () => _applyCustomShape(cols, rows, areaSize),
                child: const Text('应用'),
              ),
            ],
          );
        },
      ),
    );
    WidgetsBinding.instance.addPostFrameCallback((_) {
      colsController.dispose();
      rowsController.dispose();
    });
  }

  void _applyCustomShape(int cols, int rows, Size areaSize) {
    Navigator.of(context).pop();
    if (_grid.isCanvas) {
      setState(() {
        _grid.custom = (cols: cols, rows: rows);
        _pushUndo();
        _grid.tileCanvas(cols, rows, areaSize.width, areaSize.height);
      });
      unawaited(_saveIfChanged());
    } else {
      _mutate(() {
        _grid.custom = (cols: cols, rows: rows);
        _grid.setGridSize(cols, rows);
      });
    }
  }

  Widget _sizeBar(Size areaSize) {
    final custom = _grid.custom;
    final isCurrentCustom = custom != null &&
        ((_grid.isCanvas && _grid.canvasCols == custom.cols && _grid.canvasRows == custom.rows) ||
            (!_grid.isCanvas && _grid.cols == custom.cols && _grid.rows == custom.rows));
    return SizedBox(
      height: 40,
      child: ListView(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 8),
        children: [
          for (final size in GridState.gridSizes)
            Padding(
              padding: const EdgeInsets.only(right: 6, top: 6, bottom: 6),
              child: OutlinedButton(
                key: Key('grid-size-${size.$1}x${size.$2}'),
                style: size.$1 == _grid.cols && size.$2 == _grid.rows && !_grid.isCanvas
                    ? OutlinedButton.styleFrom(backgroundColor: AppTheme.accent.withValues(alpha: 0.15))
                    : null,
                onPressed: () => _applyShape(size.$1, size.$2, areaSize),
                child: Text('${size.$1}×${size.$2}'),
              ),
            ),
          Padding(
            padding: const EdgeInsets.only(right: 6, top: 6, bottom: 6),
            child: Tooltip(
              message: '选择行列',
              child: OutlinedButton(
                key: const Key('grid-shape-picker'),
                onPressed: () => _showShapePicker(areaSize),
                child: Text(_shapeText()),
              ),
            ),
          ),
          if (custom != null)
            Padding(
              padding: const EdgeInsets.only(right: 6, top: 6, bottom: 6),
              child: Tooltip(
                message: '自定义形状',
                child: OutlinedButton(
                  key: const Key('grid-size-custom'),
                  style: isCurrentCustom ? OutlinedButton.styleFrom(backgroundColor: AppTheme.accent.withValues(alpha: 0.15)) : null,
                  onPressed: () => _applyShape(custom.cols, custom.rows, areaSize),
                  child: Text('${custom.cols}×${custom.rows}'),
                ),
              ),
            ),
        ],
      ),
    );
  }

  Future<void> _showPicker() async {
    var query = '';
    final hosts = <({String host, Pane pane})>[];
    // Hosts were loaded into _panes without host names; reload the tree
    // here so picker rows can show which server a pane belongs to.
    try {
      final tree = await widget.api.hosts();
      for (final host in tree) {
        for (final pane in host.panes) {
          hosts.add((host: host.name, pane: pane));
        }
      }
    } catch (_) {
      for (final pane in _panes.values) {
        hosts.add((host: pane.hostId, pane: pane));
      }
    }
    if (!mounted) return;
    await showDialog(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) {
          final needle = query.trim().toLowerCase();
          final rows = hosts.where((entry) {
            if (needle.isEmpty) return true;
            final haystack =
                '${entry.host} ${entry.pane.session} ${entry.pane.windowName} ${entry.pane.command} ${entry.pane.title} ${entry.pane.path}'
                    .toLowerCase();
            return haystack.contains(needle);
          }).take(300).toList();
          return AlertDialog(
            title: const Text('添加窗口'),
            content: SizedBox(
              width: 400,
              height: 440,
              child: Column(
                children: [
                  TextField(
                    key: const Key('grid-picker-search'),
                    decoration: const InputDecoration(hintText: '搜索服务器 / session / pane / 路径'),
                    onChanged: (value) => setDialogState(() => query = value),
                  ),
                  const SizedBox(height: 10),
                  Expanded(
                    child: rows.isEmpty
                        ? const Padding(padding: EdgeInsets.all(18), child: Text('没有匹配的会话'))
                        : ListView.builder(
                            itemCount: rows.length,
                            itemBuilder: (context, index) {
                              final entry = rows[index];
                              final added = _grid.panes.contains(entry.pane.ref);
                              final pane = entry.pane;
                              final name =
                                  '${entry.host} · ${pane.session}${pane.windowName.isNotEmpty ? ':${pane.windowName}' : ''} · ${pane.command}';
                              return ListTile(
                                key: Key('grid-pick-${pane.ref}'),
                                enabled: !added,
                                title: Text(name, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13)),
                                subtitle: Text(pane.title.isNotEmpty ? pane.title : (pane.path.isNotEmpty ? pane.path : pane.ref),
                                    maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 11)),
                                trailing: added ? const Text('已在', style: TextStyle(fontSize: 11)) : null,
                                onTap: added
                                    ? null
                                    : () {
                                        _mutate(() {
                                          _grid.add(pane.ref);
                                          _grid.applyDefaultModes({pane.ref: pane.hasTranscript});
                                        });
                                        Navigator.of(context).pop();
                                      },
                              );
                            },
                          ),
                  ),
                ],
              ),
            ),
            actions: [
              TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('关闭')),
            ],
          );
        },
      ),
    );
  }

  Future<void> _showPresets() async {
    final nameController = TextEditingController();
    await showDialog(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) {
          final names = _presets.keys.toList()..sort();
          return AlertDialog(
            title: const Text('布局预设'),
            content: SizedBox(
              width: 400,
              height: 420,
              child: Column(
                children: [
                  // No Row+Expanded here: AlertDialog measures content
                  // through an IntrinsicWidth, and a flex row forwards
                  // an unbounded width query into the button, which
                  // cannot be laid out with infinite width.
                  TextField(
                    key: const Key('grid-preset-name'),
                    controller: nameController,
                    decoration: const InputDecoration(hintText: '给当前布局起个名字'),
                  ),
                  const SizedBox(height: 8),
                  Align(
                    alignment: Alignment.centerRight,
                    child: FilledButton(
                      key: const Key('grid-preset-save'),
                      onPressed: () {
                        final name = nameController.text.trim();
                        if (name.isEmpty || _grid.panes.isEmpty) return;
                        setDialogState(() {
                          _presets[name] = _grid.presetSnapshot();
                        });
                        unawaited(_savePresets());
                        nameController.clear();
                      },
                      child: const Text('保存当前'),
                    ),
                  ),
                  const SizedBox(height: 10),
                  Expanded(
                    child: names.isEmpty
                        ? const Padding(
                            padding: EdgeInsets.all(12),
                            child: Text('还没有预设：排好窗口后点「保存当前」', style: TextStyle(fontSize: 12)),
                          )
                        : ListView.builder(
                            itemCount: names.length,
                            itemBuilder: (context, index) {
                              final name = names[index];
                              final preset = _presets[name] ?? {};
                              final count = (preset['panes'] is List) ? (preset['panes'] as List).length : 0;
                              final layoutLabel = preset['layout'] == 'grid'
                                  ? '网格'
                                  : preset['layout'] == 'canvas'
                                      ? '画布'
                                      : '并排';
                              return ListTile(
                                title: Text(name, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13)),
                                subtitle: Text('$count 窗口 · $layoutLabel', style: const TextStyle(fontSize: 11)),
                                trailing: Row(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    TextButton(
                                      key: Key('grid-preset-apply-$name'),
                                      onPressed: () {
                                        _mutate(() => _grid.applyPreset(preset));
                                        Navigator.of(context).pop();
                                      },
                                      child: const Text('应用'),
                                    ),
                                    IconButton(
                                      key: Key('grid-preset-delete-$name'),
                                      tooltip: '删除',
                                      iconSize: 16,
                                      visualDensity: VisualDensity.compact,
                                      onPressed: () {
                                        setDialogState(() => _presets.remove(name));
                                        unawaited(_savePresets());
                                      },
                                      icon: const Icon(Icons.close, size: 16),
                                    ),
                                  ],
                                ),
                              );
                            },
                          ),
                  ),
                ],
              ),
            ),
            actions: [
              TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('关闭')),
            ],
          );
        },
      ),
    );
    // The dialog route is still animating out when showDialog's
    // future completes, so disposing synchronously can catch a
    // TextField mid-rebuild. Defer it past the pending frame.
    WidgetsBinding.instance.addPostFrameCallback((_) => nameController.dispose());
  }

  Widget _layoutButton(String label, String layout, Key key) {
    final active = _grid.layout == layout;
    return TextButton(
      key: key,
      onPressed: () => _mutate(() => _grid.setLayout(layout)),
      style: TextButton.styleFrom(foregroundColor: active ? AppTheme.accent : null),
      child: Text(label, style: TextStyle(fontWeight: active ? FontWeight.w700 : FontWeight.w400)),
    );
  }

  @override
  Widget build(BuildContext context) {
    final count = _grid.panes.isEmpty ? '' : '${_grid.panes.length} 个窗口';
    // Web grid.html:228: one column only on a phone, so the layout
    // switch would be a lie — hide it (and the size bar) at ≤760px.
    final narrowAppBar = MediaQuery.sizeOf(context).width <= _narrowWidth;
    return Focus(
      onKeyEvent: _onKeyEvent,
      child: Scaffold(
        key: _scaffoldKey,
        drawer: _gridDrawer(),
        appBar: AppBar(
          title: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text('多窗口'),
              if (count.isNotEmpty)
                Text(count, style: TextStyle(fontSize: 11, fontWeight: FontWeight.w400, color: AppTheme.faint)),
            ],
          ),
          actions: [
            if (!narrowAppBar) ...[
              _layoutButton('并排', 'strip', const Key('grid-layout-strip')),
              _layoutButton('网格', 'grid', const Key('grid-layout-grid')),
              _layoutButton('画布', 'canvas', const Key('grid-layout-canvas')),
            ],
            if (_grid.isCanvas) ...[
              IconButton(
                key: const Key('grid-tidy'),
                tooltip: '整理窗口',
                onPressed: () {
                  final size = _lastCanvasSize;
                  final shape = GridState.bestShape(_grid.panes.length, size.width, size.height);
                  setState(() {
                    _pushUndo();
                    _grid.tileCanvas(shape.cols, shape.rows, size.width, size.height);
                  });
                  unawaited(_saveIfChanged());
                },
                icon: const Icon(Icons.grid_view, size: 20),
              ),
              if (_undoSnapshot != null)
                IconButton(
                  key: const Key('grid-undo'),
                  tooltip: '撤销整理（Ctrl/⌘+Z）',
                  onPressed: _undo,
                  icon: const Icon(Icons.undo, size: 20),
                ),
            ],
            IconButton(
              key: const Key('grid-add'),
              tooltip: '添加窗口',
              onPressed: _showPicker,
              icon: const Icon(Icons.add, size: 20),
            ),
            IconButton(
              key: const Key('grid-presets'),
              tooltip: '预设',
              onPressed: _showPresets,
              icon: const Icon(Icons.bookmark_outline, size: 20),
            ),
            // Web's plugins-ui.js buttons on the grid surface; renders
            // nothing when the server reports no buttons.
            PluginButtons(
                api: widget.api,
                baseUrl: widget.baseUrl,
                cookieHeader: widget.api is TerminalApi
                    ? (widget.api as TerminalApi).wsHeaders['Cookie'] ?? ''
                    : ''),
          ],
        ),
        body: _grid.panes.isEmpty
            ? Center(
                child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Text('还没有窗口', style: TextStyle(fontSize: 22, fontWeight: FontWeight.w500)),
                      const SizedBox(height: 6),
                      Text('点「添加窗口」把几个会话并排放在这里，拖动分隔线可以调宽窄。',
                          textAlign: TextAlign.center, style: TextStyle(color: AppTheme.muted)),
                    ],
                  ),
                ),
              )
            : LayoutBuilder(
                builder: (context, constraints) {
                  final narrow = constraints.maxWidth <= _narrowWidth;
                  final areaSize = Size(constraints.maxWidth, constraints.maxHeight);
                  return Column(
                    children: [
                      if (!narrow && _grid.layout != 'strip') _sizeBar(areaSize),
                      Expanded(
                        child: narrow
                            ? _narrowLayout(constraints.maxHeight)
                            : _grid.isCanvas
                                ? _canvasLayout(constraints)
                                : _grid.isGrid
                                    ? _gridLayout(_grid.cols, constraints.maxWidth, constraints.maxHeight,
                                        showGrips: _grid.maximized == null)
                                    : _stripLayout(),
                      ),
                    ],
                  );
                },
              ),
      ),
    );
  }
}

/// Pulsing busy dot, web grid.html:62 `.dot.busy` + dot-pulse (1.4s).
class _BusyDot extends StatefulWidget {
  const _BusyDot();

  @override
  State<_BusyDot> createState() => _BusyDotState();
}

class _BusyDotState extends State<_BusyDot> with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1400),
  )..repeat();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, child) {
        // 0 → 3px ring, alpha 55% → 22%, back — one pulse per cycle.
        final pulse = (1 - (_controller.value * 2 - 1).abs());
        return Container(
          width: 7,
          height: 7,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: _amber,
            boxShadow: [
              BoxShadow(
                color: _amber.withValues(alpha: 0.55 - 0.33 * pulse),
                spreadRadius: 3 * pulse,
              ),
            ],
          ),
        );
      },
    );
  }
}

/// Folded-corner page glyph, web's #i-page (grid.html:261,714):
/// a page outline with the top-right corner folded over, plus one
/// text line. Distinct from Icons.open_in_new by design.
class _PageGlyph extends StatelessWidget {
  const _PageGlyph();

  @override
  Widget build(BuildContext context) {
    final color = IconTheme.of(context).color ?? Colors.black54;
    return CustomPaint(
      size: const Size(16, 16),
      painter: _PageGlyphPainter(color),
    );
  }
}

class _PageGlyphPainter extends CustomPainter {
  _PageGlyphPainter(this.color);

  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.4
      ..strokeJoin = StrokeJoin.round;
    final path = Path()
      ..moveTo(3, 2)
      ..lineTo(10, 2)
      ..lineTo(14, 6)
      ..lineTo(14, 14)
      ..lineTo(3, 14)
      ..close()
      ..moveTo(10, 2)
      ..lineTo(10, 6)
      ..lineTo(14, 6)
      ..moveTo(5.5, 9)
      ..lineTo(11.5, 9);
    canvas.drawPath(path, paint);
  }

  @override
  bool shouldRepaint(_PageGlyphPainter oldDelegate) => oldDelegate.color != color;
}
