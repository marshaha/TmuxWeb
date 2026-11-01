import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../models/grid_state.dart';
import '../models/models.dart';
import '../theme.dart';
import 'chat_screen.dart';
import 'terminal_screen.dart';

const _amber = Color(0xFFD89A3C);

/// Multi-window page, mirroring web/grid.html: several panes side by
/// side (strip) or in a grid, each cell a live chat or terminal, with
/// per-cell mode switching, maximize, drag reorder, and named
/// presets. State lives in SharedPreferences under the same keys web
/// uses, so settings export/import moves it verbatim.
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
  SharedPreferences? _prefs;
  Timer? _statusTimer;

  @override
  void initState() {
    super.initState();
    _init();
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    super.dispose();
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
    if (mounted) {
      setState(() {
        _grid = restored;
        _presets = presets;
      });
    }
    // 先装定时器再 await：等主机列表回来才创建的话，用户中途退出时 dispose 已经跑过，
    // 这个 10 秒轮询会跟着进程活到天荒地老。
    _statusTimer = Timer.periodic(const Duration(seconds: 10), (_) => _loadStatuses());
    await _loadHosts();
    await _loadStatuses();
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
      await _save();
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
      if (changed) setState(() => _statuses = statuses);
    } catch (_) {
      // Status dots are best-effort; a failed sweep leaves the old ones.
    }
  }

  Future<void> _save() async {
    await _prefs?.setString(GridState.gridKey, jsonEncode(_grid.toJson()));
  }

  Future<void> _savePresets() async {
    await _prefs?.setString(GridState.presetsKey, jsonEncode(_presets));
  }

  String _titleFor(String ref) {
    final pane = _panes[ref];
    if (pane == null) return ref;
    final base = pane.session.isNotEmpty ? pane.session : ref;
    final window = pane.windowName.isNotEmpty ? ':${pane.windowName}' : '';
    final command = pane.command.isNotEmpty ? ' · ${pane.command}' : '';
    final title = '$base$window$command';
    return title.isNotEmpty ? title : pane.label;
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
    unawaited(_save());
  }

  void _toggleMax(String ref) {
    setState(() => _grid.maximized = _grid.maximized == ref ? null : ref);
  }

  Widget _cellBody(String ref) {
    final pane = _panes[ref] ?? Pane(ref: ref, title: ref, command: '');
    if (_grid.modeFor(ref) == 'term') {
      final socketApi = widget.api is TerminalApi ? widget.api as TerminalApi : null;
      if (socketApi == null) return const Center(child: Text('终端需要 socket 支持'));
      return TerminalScreen(api: socketApi, pane: pane, embedded: true);
    }
    return ChatScreen(
      source: PaneChatSource(api: widget.api, pane: pane),
      terminalApi: widget.api is TerminalApi ? widget.api as TerminalApi : null,
      embedded: true,
      baseUrl: widget.baseUrl,
    );
  }

  Widget _cellHeader(String ref) {
    final mode = _grid.modeFor(ref);
    final maximized = _grid.maximized == ref;
    // No double-tap-to-maximize wrapper here: it would put a
    // double-tap recognizer above the header buttons, delaying every
    // button tap by the double-tap timeout (and swallowing taps in
    // widget tests, where the timeout never advances on its own).
    // The maximize button covers the same action explicitly.
    return Container(
      height: 34,
      padding: const EdgeInsets.only(left: 10, right: 6),
      decoration: BoxDecoration(
        color: AppTheme.page,
        border: Border(bottom: BorderSide(color: AppTheme.line)),
      ),
        child: Row(
          children: [
            Container(
              width: 7,
              height: 7,
              decoration: BoxDecoration(shape: BoxShape.circle, color: _dotColor(ref)),
            ),
            const SizedBox(width: 7),
            Expanded(
              child: Text(_titleFor(ref),
                  maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w600)),
            ),
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
              onPressed: () {
                final pane = _panes[ref] ?? Pane(ref: ref, title: ref, command: '');
                Navigator.of(context).push(MaterialPageRoute(
                  builder: (_) => ChatScreen(
                    source: PaneChatSource(api: widget.api, pane: pane),
                    terminalApi: widget.api is TerminalApi ? widget.api as TerminalApi : null,
                    baseUrl: widget.baseUrl,
                  ),
                ));
              },
              icon: const Icon(Icons.open_in_new, size: 16),
            ),
            IconButton(
              key: Key('grid-max-$ref'),
              tooltip: maximized ? '恢复' : '最大化',
              iconSize: 16,
              visualDensity: VisualDensity.compact,
              onPressed: () => _toggleMax(ref),
              icon: Icon(maximized ? Icons.fullscreen_exit : Icons.fullscreen, size: 16),
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
          onHorizontalDragEnd: (_) => _save(),
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

  Widget _gridLayout(int effectiveCols) {
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
          for (var r = 0; r < rows.length; r++)
            Expanded(
              flex: flexOf(_grid.rowFr.length > r ? _grid.rowFr[r] : 1),
              child: Padding(
                padding: EdgeInsets.only(bottom: r < rows.length - 1 ? 8 : 0),
                child: Row(
                  children: [
                    for (var c = 0; c < effectiveCols; c++)
                      Expanded(
                        flex: flexOf(_grid.colFr.length > c ? _grid.colFr[c] : 1),
                        child: Padding(
                          padding: EdgeInsets.only(right: c < effectiveCols - 1 ? 8 : 0),
                          child: c < rows[r].length ? _draggableCell(rows[r][c]) : const SizedBox(),
                        ),
                      ),
                  ],
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
                              final layoutLabel = preset['layout'] == 'grid' ? '网格' : '并排';
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
    return Scaffold(
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
          _layoutButton('并排', 'strip', const Key('grid-layout-strip')),
          _layoutButton('网格', 'grid', const Key('grid-layout-grid')),
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
                final narrow = constraints.maxWidth < 600;
                final effectiveCols = narrow ? 1 : _grid.cols;
                return Column(
                  children: [
                    if (_grid.isGrid)
                      SizedBox(
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
                                  style: size.$1 == _grid.cols && size.$2 == _grid.rows
                                      ? OutlinedButton.styleFrom(backgroundColor: AppTheme.accent.withValues(alpha: 0.15))
                                      : null,
                                  onPressed: () => _mutate(() => _grid.setGridSize(size.$1, size.$2)),
                                  child: Text('${size.$1}×${size.$2}'),
                                ),
                              ),
                          ],
                        ),
                      ),
                    Expanded(
                      child: _grid.isGrid ? _gridLayout(effectiveCols) : _stripLayout(),
                    ),
                  ],
                );
              },
            ),
    );
  }
}
