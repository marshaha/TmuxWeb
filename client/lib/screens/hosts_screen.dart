import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'dart:async';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../models/models.dart';
import '../theme.dart';
import '../widgets/pane_hover_preview.dart';
import '../widgets/recent_block.dart';
import '../widgets/sidebar_file_panel.dart';
import 'chat_screen.dart';
import '../widgets/plugin_buttons.dart';
import 'grid_screen.dart';
import 'home_board_screen.dart';
import 'hosts_admin_screen.dart';
import 'settings_screen.dart';
import 'terminal_screen.dart';

/// Host → pane browser. Narrow screens keep the mobile flow (the tree is a
/// full-screen page, taps push); on wide screens (>= 861px, web's own
/// narrow breakpoint) this becomes the web console shell: a fixed 280px
/// sidebar on the left and a detail pane on the right, like web's
/// `grid-template-columns: var(--side-w,280px) minmax(0,1fr)`.
///
/// Web parity notes: sidebar width 280px is web's --side-w default; the
/// detail defaults to the home board and shows an embedded chat for the
/// selected pane (web: the sidebar + conversation). Drag-resizing the
/// sidebar (web's side-resizer, shell.js) and collapsing it are in this
/// cut: drag the handle, double-click it to reset to 280, both persisted.
class HostsScreen extends StatefulWidget {
  const HostsScreen(
      {super.key, required this.api, required this.baseUrl, this.initialPaneRef});

  final Api api;
  final String baseUrl;

  /// Pane ref to select once hosts load (grid "在控制台打开" and
  /// terminal "在控制台打开" deep links). Wide shell shows it in the
  /// detail pane; narrow auto-opens its chat once, post-frame (see
  /// [_maybeAutoOpenDeepLink]), while still highlighting its tree row
  /// underneath.
  final String? initialPaneRef;

  @override
  State<HostsScreen> createState() => _HostsScreenState();
}

class _HostsScreenState extends State<HostsScreen> {
  List<Host>? _hosts;
  Map<String, String> _states = const {};
  Set<String> _favorites = const {};
  String? _error;
  bool _loading = true;
  Timer? _stateTimer;

  /// Wide-shell only: the pane shown in the detail pane. Null means the
  /// detail shows the home board. Never set on narrow screens (taps push).
  Pane? _selectedPane;

  /// Whether the [HostsScreen.initialPaneRef] deep link has been applied.
  bool _initialPaneApplied = false;

  /// Narrow deep link: pane refs already auto-opened, and whether a
  /// deep-link push is currently in flight. A ref auto-opens at most
  /// once, and a new ref arriving while a push is in flight is ignored
  /// rather than stacked on top of it, so rapid parent rebuilds cannot
  /// double-push or crash the navigator.
  final Set<String> _deepLinkOpened = {};
  bool _deepLinkPushInFlight = false;

  /// Wide-shell sidebar file panel (web's console `#file-panel`):
  /// whether the panel replaces the tree, and the last directory it
  /// resolved to for the selected pane (null = the pane's own directory).
  /// Narrow has no persistent sidebar in Flutter — the tree is a
  /// full-screen page — so the panel lives in the wide shell only.
  bool _filesMode = false;
  String? _filesPath;

  /// Whether 设置 → 侧栏「按 pane 记住」 is on, from the local mirror
  /// SharedPreferences 'tmux-sidebar-memory' == 'pane' (synced to the
  /// account as /api/prefs sidebar: pane|none). Read fresh whenever a
  /// sidebar view is applied or written, so returning from Settings
  /// never acts on a stale cached value.
  bool _sidebarMemoryOn = false;

  /// Web's narrow breakpoint: at or below 860px the sidebar is an overlay.
  static const double _wideBreakpoint = 861;

  /// Web's --side-w default for the console sidebar.
  static const double _sidebarDefaultWidth = 280;

  /// Current sidebar width (drag-resized, persisted as tmux-side-w).
  double _sidebarWidth = _sidebarDefaultWidth;

  /// Collapsed sidebar (persisted as tmux-sidebar-collapsed).
  bool _sidebarCollapsed = false;

  /// Pin labels (pane ref → label) for nameForPane, from /api/home/recent.
  Map<String, String> _pinLabels = const {};

  /// Favorite titles (pane ref → title) for nameForPane.
  Map<String, String> _favoriteTitles = const {};

  double? _dragStartWidth;
  double _dragStartX = 0;

  @override
  void initState() {
    super.initState();
    _load();
    _loadFavorites();
    _loadPins();
    _loadSidebarPrefs();
    // Account preview-delay wins over the local mirror (settings.js
    // loadPreviewPref); hover reads the mirror from SharedPreferences.
    unawaited(PaneHoverPreview.syncDelayFromAccount(widget.api));
    _stateTimer = Timer.periodic(const Duration(seconds: 9), (_) => _refreshStates());
  }

  Future<void> _loadSidebarPrefs() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      if (!mounted) return;
      setState(() {
        _sidebarWidth = (prefs.getDouble('tmux-side-w') ?? _sidebarDefaultWidth).clamp(200, 640);
        _sidebarCollapsed = prefs.getBool('tmux-sidebar-collapsed') ?? false;
      });
      _sidebarMemoryOn = prefs.getString('tmux-sidebar-memory') == 'pane';
    } catch (_) {}
  }

  /// Fresh read of the sidebar-memory mirror (see [_sidebarMemoryOn]).
  Future<bool> _readSidebarMemory() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final on = prefs.getString('tmux-sidebar-memory') == 'pane';
      _sidebarMemoryOn = on;
      return on;
    } catch (_) {
      return _sidebarMemoryOn;
    }
  }

  /// Web's selectPane sidebar branch (web/index.html, 2c282a4), applied
  /// whenever the selected pane changes:
  /// - memory on: an unrecorded pane (or one last closed) shows the
  ///   session tree — never the previous pane's panel — and a recorded
  ///   open panel is restored at its directory; the view is written
  ///   back so the switch itself is remembered.
  /// - memory off: a closed panel stays closed; an open panel follows
  ///   the new pane at that pane's own directory (web: openFiles(null)).
  /// A fresh shell starts closed either way, matching web's
  /// `filesMode:false` initial state.
  Future<void> _applySidebarViewForPane(Pane pane) async {
    final memoryOn = await _readSidebarMemory();
    if (!mounted) return;
    if (memoryOn) {
      final views = await SidebarViewMemory.load();
      if (!mounted) return;
      final remembered = SidebarFileView.tryParse(views[pane.ref]);
      if (remembered != null && remembered.open) {
        final path = remembered.path.isEmpty ? null : remembered.path;
        setState(() {
          _filesMode = true;
          _filesPath = path;
        });
        await SidebarViewMemory.save(pane.ref, SidebarFileView(open: true, path: remembered.path).encode());
      } else {
        setState(() {
          _filesMode = false;
          _filesPath = null;
        });
        await SidebarViewMemory.save(pane.ref, const SidebarFileView(open: false).encode());
      }
    } else {
      // Panel state itself is unchanged; only the directory resets so
      // an open panel remounts at the new pane's directory.
      setState(() => _filesPath = null);
    }
  }

  /// The panel's folder toggle (web's `#files-toggle`): with no pane
  /// selected there is nothing to show, like web's openFiles guard.
  /// Closing writes the view only when memory is on — web's
  /// writeFilesView is likewise a no-op when memory is off.
  Future<void> _toggleFilesPanel() async {
    final pane = _selectedPane;
    if (pane == null) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('先选择一个 pane')));
      return;
    }
    final next = !_filesMode;
    setState(() => _filesMode = next);
    if (await _readSidebarMemory()) {
      await SidebarViewMemory.save(pane.ref, SidebarFileView(open: next, path: _filesPath ?? '').encode());
    }
  }

  /// The panel's second write (web's openFiles post-resolve write):
  /// browsing into a directory updates the remembered directory while
  /// the panel stays open. No write when memory is off.
  void _onSidebarDirectory(String path) {
    _filesPath = path;
    final pane = _selectedPane;
    if (pane == null) return;
    unawaited(_writeSidebarViewIfMemoryOn(pane, SidebarFileView(open: true, path: path)));
  }

  Future<void> _writeSidebarViewIfMemoryOn(Pane pane, SidebarFileView view) async {
    if (await _readSidebarMemory()) {
      await SidebarViewMemory.save(pane.ref, view.encode());
    }
  }

  Future<void> _saveSidebarPrefs() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setDouble('tmux-side-w', _sidebarWidth);
      await prefs.setBool('tmux-sidebar-collapsed', _sidebarCollapsed);
    } catch (_) {}
  }

  /// Pin labels for the tree titles and the recent block. Silent on
  /// failure, like _loadFavorites: names just fall back.
  Future<void> _loadPins() async {
    try {
      final data = await widget.api.pins();
      final labels = <String, String>{};
      final pinned = data['pinned'];
      if (pinned is List) {
        for (final row in pinned) {
          if (row is Map) {
            final ref = '${row['pane'] ?? ''}';
            final label = '${row['label'] ?? ''}';
            if (ref.isNotEmpty && label.isNotEmpty) labels[ref] = label;
          }
        }
      }
      if (mounted) setState(() => _pinLabels = labels);
    } catch (_) {}
  }

  /// Web's shell loadFavorites: silent on failure, so a board error never
  /// breaks the tree; the star just reads unstarred until the next load.
  Future<void> _loadFavorites() async {
    try {
      final data = await widget.api.homeFavorites();
      final list = data['favorites'];
      if (list is! List || !mounted) return;
      final titles = <String, String>{};
      for (final item in list) {
        if (item is Map) {
          final ref = '${item['pane'] ?? ''}';
          final title = '${item['title'] ?? ''}';
          if (ref.isNotEmpty && title.isNotEmpty) titles[ref] = title;
        }
      }
      setState(() {
        _favorites = list
            .map((item) => item is Map ? '${item['pane'] ?? ''}' : '$item')
            .where((pane) => pane.isNotEmpty)
            .toSet();
        _favoriteTitles = titles;
      });
    } catch (_) {}
  }

  /// Web's shell toggleStar: DELETE when starred, POST {pane} otherwise,
  /// then reload the set so the row reflects the server, not optimism.
  Future<void> _toggleFavorite(Pane pane) => _toggleFavoriteRef(pane.ref);

  Future<void> _toggleFavoriteRef(String ref) async {
    final starred = _favorites.contains(ref);
    try {
      if (starred) {
        await widget.api.deleteHomeFavorite(ref);
      } else {
        await widget.api.addHomeFavorite(ref);
      }
      await _loadFavorites();
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(starred ? '已取消收藏' : '已收藏到主页')));
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
    }
  }

  @override
  void dispose() {
    _stateTimer?.cancel();
    super.dispose();
  }

  @override
  void didUpdateWidget(HostsScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.initialPaneRef == oldWidget.initialPaneRef) return;
    // A changed deep link re-arms the one-shot application: the load
    // that already ran must not swallow the new value, and a load still
    // in flight reads the new widget value when it lands.
    _initialPaneApplied = false;
    final ref = widget.initialPaneRef;
    final hosts = _hosts;
    if (ref == null || hosts == null) return;
    final target = _findPane(ref);
    if (target == null) return;
    _initialPaneApplied = true;
    setState(() => _selectedPane = target);
    _maybeAutoOpenDeepLink(target);
    unawaited(_applySidebarViewForPane(target));
  }

  Pane? _findPane(String ref) {
    for (final host in _hosts ?? const <Host>[]) {
      for (final pane in host.panes) {
        if (pane.ref == ref) return pane;
      }
    }
    return null;
  }

  Future<void> _refreshStates() async {
    try {
      final states = await widget.api.paneStates();
      if (mounted) setState(() => _states = states);
    } catch (_) {
      // the probe is best-effort: a host that is offline just has no dot
    }
  }

  Future<void> _load() async {
    setState(() => _loading = true);
    try {
      final hosts = await widget.api.hosts();
      if (!mounted) return;
      Pane? deepLinked;
      setState(() {
        _hosts = hosts;
        _error = null;
        _loading = false;
        // Prune a wide-shell selection whose pane went away with the reload.
        final selected = _selectedPane;
        if (selected != null && !hosts.any((host) => host.panes.any((pane) => pane.ref == selected.ref))) {
          _selectedPane = null;
          _filesMode = false;
        }
        // Deep-link selection, applied once on the first load that has
        // hosts; later reloads never override the user's own selection.
        if (!_initialPaneApplied) {
          _initialPaneApplied = true;
          final ref = widget.initialPaneRef;
          if (ref != null && _selectedPane == null) {
            for (final host in hosts) {
              for (final pane in host.panes) {
                if (pane.ref == ref) {
                  _selectedPane = pane;
                  deepLinked = pane;
                }
              }
            }
          }
        }
      });
      final deepLinkedPane = deepLinked;
      if (deepLinkedPane != null) {
        _maybeAutoOpenDeepLink(deepLinkedPane);
        unawaited(_applySidebarViewForPane(deepLinkedPane));
      }
      unawaited(_refreshStates());
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _error = '$error';
        _loading = false;
      });
    }
  }

  int get _paneCount => (_hosts ?? const <Host>[]).fold<int>(0, (sum, host) => sum + host.panes.length);

  void _openPane(Pane pane) {
    if (MediaQuery.sizeOf(context).width >= _wideBreakpoint) {
      // Wide shell: select into the detail pane instead of pushing.
      setState(() => _selectedPane = pane);
      unawaited(_applySidebarViewForPane(pane));
      return;
    }
    // Narrow has no sidebar to show, but the view is still applied and
    // written like web's selectPane, so it is correct if the shell
    // later goes wide (and so a recorded panel is not lost by a tap).
    setState(() => _selectedPane = pane);
    unawaited(_applySidebarViewForPane(pane));
    unawaited(_pushChatPage(pane));
  }

  Future<void> _pushChatPage(Pane pane) {
    final api = widget.api;
    return Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: api is TerminalApi ? api as TerminalApi : null,
        baseUrl: widget.baseUrl,
      ),
    ));
  }

  /// Narrow deep link: schedule the chat push for the next frame, never
  /// during build (a push during build throws "setState() or
  /// markNeedsBuild() called during build"). The width is re-checked in
  /// the callback itself, so a shell that has since grown wide keeps
  /// its detail pane instead of pushing. The push is "in flight" until
  /// the chat page pops, so a second deep-link value arriving in the
  /// meantime is dropped rather than stacked; the per-value set keeps
  /// a later reload or parent rebuild from re-pushing the same ref.
  void _maybeAutoOpenDeepLink(Pane pane) {
    final ref = widget.initialPaneRef;
    if (ref == null || pane.ref != ref) return;
    if (_deepLinkOpened.contains(ref) || _deepLinkPushInFlight) return;
    _deepLinkPushInFlight = true;
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      if (!mounted) {
        _deepLinkPushInFlight = false;
        return;
      }
      if (MediaQuery.sizeOf(context).width >= _wideBreakpoint) {
        _deepLinkPushInFlight = false;
        return;
      }
      _deepLinkOpened.add(ref);
      try {
        await _pushChatPage(pane);
      } finally {
        _deepLinkPushInFlight = false;
      }
    });
  }

  /// Full-screen chat for a pane (detail header's "open full" action).
  void _openPaneFull(Pane pane) {
    final api = widget.api;
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: api is TerminalApi ? api as TerminalApi : null,
        baseUrl: widget.baseUrl,
      ),
    ));
  }

  /// New-session dialog, mirroring web/index.html: per-host '+' button,
  /// name + optional path (prefilled with the host's first pane path).
  /// On success the tree reloads and the first new pane opens, like
  /// web selecting it; without a matching pane it reports the name.
  Future<void> _newSession(Host host) async {
    final result = await showDialog<Map<String, dynamic>>(
      context: context,
      builder: (_) => _NewSessionDialog(api: widget.api, host: host),
    );
    if (result == null || !mounted) return;
    await _load();
    if (!mounted) return;
    Pane? target;
    final panes = result['panes'];
    if (panes is List && panes.isNotEmpty) {
      final ref = '${panes.first['id'] ?? panes.first['pane'] ?? ''}';
      if (ref.isNotEmpty) {
        for (final host in _hosts ?? const <Host>[]) {
          for (final pane in host.panes) {
            if (pane.ref == ref) target = pane;
          }
        }
      }
    }
    if (target != null) {
      _openPane(target);
      return;
    }
    final session = result['session'];
    final name = session is Map ? '${session['session'] ?? ''}' : '$session';
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('已创建会话 $name')));
  }

  Future<void> _openShare() async {
    final slug = TextEditingController();
    final password = TextEditingController();
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('打开分享链接'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              key: const Key('share-slug'),
              controller: slug,
              autocorrect: false,
              decoration: const InputDecoration(hintText: '分享路径，例如 share-mvqk07kw'),
            ),
            const SizedBox(height: 10),
            TextField(
              key: const Key('share-password'),
              controller: password,
              obscureText: true,
              decoration: const InputDecoration(hintText: '访问密码（可选）'),
            ),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('打开')),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    final value = slug.text.trim();
    if (value.isEmpty) return;
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ShareScreen(api: widget.api, slug: value, password: password.text),
    ));
  }

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(builder: (context, constraints) {
      if (constraints.maxWidth >= _wideBreakpoint) return _buildWide();
      return _buildNarrow();
    });
  }

  /// Narrow flow, unchanged: the tree is a full-screen page.
  Widget _buildNarrow() {
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('会话'),
            Text(widget.baseUrl, style: TextStyle(fontSize: 11, fontWeight: FontWeight.w400, color: AppTheme.faint)),
          ],
        ),
        actions: _actions(wide: false),
      ),
      body: RefreshIndicator(onRefresh: _load, child: _buildBody()),
      bottomNavigationBar: Container(
        padding: EdgeInsets.fromLTRB(16, 10, 16, 10 + MediaQuery.of(context).padding.bottom),
        decoration: BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
        child: Text(
          _hosts == null ? '' : '${_hosts!.length} 台主机 · $_paneCount 个 pane',
          style: TextStyle(fontSize: 11.5, color: AppTheme.faint),
        ),
      ),
    );
  }

  /// Shared header actions. Wide mode differs in one place: the board
  /// button shows the board in the detail pane instead of pushing a page.
  List<Widget> _actions({required bool wide}) {
    final density = wide ? VisualDensity.compact : null;
    final api = widget.api;
    final cookie = api is TerminalApi
        ? (api as TerminalApi).wsHeaders['Cookie'] ?? ''
        : '';
    return [
      IconButton(
        key: const Key('grid'),
        tooltip: '多窗口',
        visualDensity: density,
        onPressed: () => Navigator.of(context).push(
            MaterialPageRoute(builder: (_) => GridScreen(api: widget.api, baseUrl: widget.baseUrl))),
        icon: const Icon(Icons.grid_view_outlined, size: 20),
      ),
      IconButton(
        key: const Key('home-board'),
        tooltip: '主页板',
        visualDensity: density,
        onPressed: wide
            ? () => setState(() {
                  _selectedPane = null;
                  _filesMode = false;
                })
            : () => Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => HomeBoardScreen(api: widget.api, baseUrl: widget.baseUrl))),
        icon: Icon(
          Icons.dashboard_outlined,
          size: 20,
          color: wide && _selectedPane == null ? AppTheme.accent : null,
        ),
      ),
      IconButton(
        key: const Key('settings'),
        tooltip: '设置',
        visualDensity: density,
        onPressed: () async {
          await Navigator.of(context).push(MaterialPageRoute(
            builder: (_) => SettingsScreen(api: widget.api, baseUrl: widget.baseUrl),
          ));
          await _load();
        },
        icon: const Icon(Icons.settings_outlined, size: 20),
      ),
      IconButton(
        key: const Key('hosts-admin'),
        tooltip: '服务器管理',
        visualDensity: density,
        onPressed: () async {
          await Navigator.of(context).push(MaterialPageRoute(
            builder: (_) => HostsAdminScreen(api: widget.api),
          ));
          await _load();
        },
        icon: const Icon(Icons.dns_outlined, size: 20),
      ),
      IconButton(
          tooltip: '打开分享链接', visualDensity: density, onPressed: _openShare, icon: const Icon(Icons.link, size: 20)),
      IconButton(tooltip: '刷新', visualDensity: density, onPressed: _load, icon: const Icon(Icons.refresh, size: 20)),
      // Web's plugins-ui.js buttons, appended to the console's action
      // row; renders nothing when the server reports no buttons.
      PluginButtons(
          api: api, baseUrl: widget.baseUrl, cookieHeader: cookie),
    ];
  }

  /// Wide shell: resizable + collapsible sidebar + detail pane,
  /// mirroring web's console grid. The 861px breakpoint is unchanged.
  Widget _buildWide() {
    if (_sidebarCollapsed) {
      return Scaffold(
        body: Stack(
          children: [
            _buildDetail(),
            Positioned(
              top: 8,
              left: 8,
              child: IconButton(
                key: const Key('sidebar-expand'),
                tooltip: '展开侧栏',
                icon: const Icon(Icons.chevron_right, size: 20),
                onPressed: () {
                  setState(() => _sidebarCollapsed = false);
                  _saveSidebarPrefs();
                },
              ),
            ),
          ],
        ),
      );
    }
    return Scaffold(
      body: Row(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            width: _sidebarWidth,
            decoration: BoxDecoration(
              color: AppTheme.sidebar,
              border: Border(right: BorderSide(color: AppTheme.line)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 16, 8, 8),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          const Expanded(
                            child: Text('会话', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
                          ),
                          // Web's console `#files-toggle`: the file panel
                          // replaces the tree while open (see below).
                          IconButton(
                            key: const Key('sidebar-files-toggle'),
                            tooltip: _filesMode ? '隐藏文件面板' : '文件面板',
                            iconSize: 18,
                            visualDensity: VisualDensity.compact,
                            icon: Icon(Icons.folder_outlined,
                                size: 18, color: _filesMode ? AppTheme.accent : null),
                            onPressed: _toggleFilesPanel,
                          ),
                          IconButton(
                            key: const Key('sidebar-collapse'),
                            tooltip: '折叠侧栏',
                            iconSize: 18,
                            visualDensity: VisualDensity.compact,
                            icon: const Icon(Icons.chevron_left, size: 18),
                            onPressed: () {
                              setState(() => _sidebarCollapsed = true);
                              _saveSidebarPrefs();
                            },
                          ),
                        ],
                      ),
                      const SizedBox(height: 2),
                      Text(
                        widget.baseUrl,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(fontSize: 11, color: AppTheme.faint),
                      ),
                      const SizedBox(height: 8),
                      Wrap(children: _actions(wide: true)),
                    ],
                  ),
                ),
                Expanded(
                  child: _filesMode && _selectedPane != null
                      ? SidebarFilePanel(
                          key: ValueKey('sidebar-files-${_selectedPane!.ref}'),
                          api: widget.api,
                          pane: _selectedPane!,
                          initialPath: _filesPath,
                          onClose: () => unawaited(_toggleFilesPanel()),
                          onDirectoryChanged: _onSidebarDirectory,
                        )
                      : RefreshIndicator(onRefresh: _load, child: _buildBody()),
                ),
                RecentBlock(
                  load: _loadRecentBlock,
                  onOpen: _openRecentPane,
                  onTogglePin: (ref, pinned) async {
                    await widget.api.pinPane(ref, pinned: pinned);
                    await _loadPins();
                  },
                  onRenamePin: (ref, label) async {
                    await widget.api.renamePin(ref, label);
                    await _loadPins();
                  },
                  onToggleFavorite: (ref, starred) async {
                    // _toggleFavoriteRef reloads the set and toasts like
                    // the tree star; the block re-reads it on refresh.
                    await _toggleFavoriteRef(ref);
                  },
                ),
                Container(
                  padding: EdgeInsets.fromLTRB(16, 10, 16, 10 + MediaQuery.of(context).padding.bottom),
                  decoration: BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
                  child: Text(
                    _hosts == null ? '' : '${_hosts!.length} 台主机 · $_paneCount 个 pane',
                    style: TextStyle(fontSize: 11.5, color: AppTheme.faint),
                  ),
                ),
              ],
            ),
          ),
          _sidebarResizer(),
          Expanded(child: _buildDetail()),
        ],
      ),
    );
  }

  /// Drag handle between sidebar and detail (web shell.js side-resizer):
  /// drag to resize (200–640px, capped at 60% of the window), double-click
  /// to reset to 280. Width persists on drag end and on reset.
  Widget _sidebarResizer() {
    return MouseRegion(
      cursor: SystemMouseCursors.resizeColumn,
      child: GestureDetector(
        key: const Key('sidebar-resizer'),
        // Record on pointer-down, not drag-start: drag-start fires
        // after the touch slop is consumed, which would eat ~20px of
        // every resize (web reads pointerdown the same way).
        onHorizontalDragDown: (details) {
          _dragStartX = details.globalPosition.dx;
          _dragStartWidth = _sidebarWidth;
        },
        onHorizontalDragUpdate: (details) {
          final start = _dragStartWidth;
          if (start == null) return;
          final maxWidth = [640.0, MediaQuery.of(context).size.width * 0.6].reduce((a, b) => a < b ? a : b);
          setState(() => _sidebarWidth = (start + details.globalPosition.dx - _dragStartX).clamp(200, maxWidth));
        },
        onHorizontalDragEnd: (_) => _saveSidebarPrefs(),
        onDoubleTap: () {
          setState(() => _sidebarWidth = _sidebarDefaultWidth);
          _saveSidebarPrefs();
        },
        child: Container(width: 8, color: Colors.transparent),
      ),
    );
  }

  /// The recent block's data: pinned rows first (their labels feed
  /// nameForPane), then recent rows, from GET /api/home/recent.
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
    _pinLabels = pinLabels;
    RecentRowData toRow(Map<String, dynamic> row, bool pinned) {
      final ref = '${row['pane'] ?? ''}';
      final rawTitle = '${row['title'] ?? ''}';
      final number = (row['number'] as num?)?.toInt();
      final visited = (row['visited_at'] as num?)?.toDouble() ?? 0;
      final inTree = _hosts?.any((host) => host.panes.any((pane) => pane.ref == ref)) ?? false;
      return RecentRowData(
        paneRef: ref,
        label: nameForPane(
            paneRef: ref,
            pinLabels: pinLabels,
            favoriteTitles: _favoriteTitles,
            fallback: rawTitle.isNotEmpty ? rawTitle : ref),
        pinLabel: pinned ? '${row['label'] ?? ''}' : '',
        numberLabel: number == null ? '' : '#$number',
        status: inTree ? (_states[ref] ?? '') : 'offline',
        visitedLabel: pinned ? '' : formatRecentVisited(visited),
        pinned: pinned,
        starred: _favorites.contains(ref),
      );
    }

    return RecentBlockData(
      pinned: [for (final row in pinnedRaw) if ('${row['pane'] ?? ''}'.isNotEmpty) toRow(row, true)],
      recent: [for (final row in rawRows(data['recent'])) if ('${row['pane'] ?? ''}'.isNotEmpty) toRow(row, false)],
    );
  }

  void _openRecentPane(String paneRef) {
    for (final host in _hosts ?? const <Host>[]) {
      for (final pane in host.panes) {
        if (pane.ref == paneRef) {
          _openPane(pane);
          return;
        }
      }
    }
  }

  /// Detail pane: the home board by default, an embedded chat for the
  /// selected pane (web: sidebar + conversation). The chat is centered
  /// at web's 980px message width; the board uses the full detail width.
  Widget _buildDetail() {
    final pane = _selectedPane;
    if (pane == null) {
      return HomeBoardScreen(api: widget.api, baseUrl: widget.baseUrl);
    }
    final api = widget.api;
    final terminalApi = api is TerminalApi ? api as TerminalApi : null;
    final subtitle = [
      pane.session,
      if (pane.windowName.isNotEmpty) pane.windowName,
      if (pane.command.isNotEmpty) pane.command,
    ].join(' · ');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(border: Border(bottom: BorderSide(color: AppTheme.line))),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                        nameForPane(
                            paneRef: pane.ref,
                            pinLabels: _pinLabels,
                            favoriteTitles: _favoriteTitles,
                            fallback: pane.label),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
                    if (subtitle.isNotEmpty)
                      Text(subtitle,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(fontSize: 11, color: AppTheme.faint)),
                  ],
                ),
              ),
              if (terminalApi != null)
                IconButton(
                  key: const Key('shell-detail-terminal'),
                  tooltip: '终端',
                  onPressed: () => Navigator.of(context).push(
                      MaterialPageRoute(
                          builder: (_) => TerminalScreen(
                                api: terminalApi,
                                pane: pane,
                                onOpenInConsole: () =>
                                    Navigator.of(context).push(
                                        MaterialPageRoute(
                                            builder: (_) => HostsScreen(
                                                api: widget.api,
                                                baseUrl: widget.baseUrl,
                                                initialPaneRef: pane.ref))),
                              ))),
                  icon: const Icon(Icons.terminal, size: 20),
                ),
              IconButton(
                key: const Key('shell-detail-open'),
                tooltip: '完整对话',
                onPressed: () => _openPaneFull(pane),
                icon: const Icon(Icons.open_in_full, size: 20),
              ),
              IconButton(
                key: const Key('shell-detail-close'),
                tooltip: '返回主页板',
                onPressed: () => setState(() {
                  _selectedPane = null;
                  _filesMode = false;
                }),
                icon: const Icon(Icons.close, size: 20),
              ),
            ],
          ),
        ),
        Expanded(
          child: Align(
            alignment: Alignment.topCenter,
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 980),
              child: ChatScreen(
                key: ValueKey('shell-chat-${pane.ref}'),
                source: PaneChatSource(api: api, pane: pane),
                terminalApi: terminalApi,
                baseUrl: widget.baseUrl,
                embedded: true,
              ),
            ),
          ),
        ),
      ],
    );
  }

  /// host → session → window grouping, stable and cheap for typical sizes.
  List<(String, List<Pane>)> _groupBySession(List<Pane> panes) {
    final map = <String, List<Pane>>{};
    for (final pane in panes) {
      final key = pane.session.isEmpty ? 'default' : pane.session;
      map.putIfAbsent(key, () => []).add(pane);
    }
    final keys = map.keys.toList()..sort();
    return [for (final key in keys) (key, map[key]!)];
  }

  List<(String, List<Pane>)> _groupByWindow(List<Pane> panes) {
    final map = <String, List<Pane>>{};
    for (final pane in panes) {
      final label = pane.windowName.isNotEmpty
          ? '${pane.index}: ${pane.windowName}'
          : (pane.index.isNotEmpty ? 'window ${pane.index}' : 'window');
      map.putIfAbsent(label, () => []).add(pane);
    }
    final keys = map.keys.toList()..sort();
    return [for (final key in keys) (key, map[key]!)];
  }

  Widget _paneTile(Pane pane) {
    final status = _states[pane.ref];
    final busy = status == 'busy';
    final displayName = nameForPane(
        paneRef: pane.ref, pinLabels: _pinLabels, favoriteTitles: _favoriteTitles, fallback: pane.label);
    final titleText = [pane.numberLabel, displayName].where((part) => part.isNotEmpty).join(' ');
    return PaneHoverPreview(
      paneRef: pane.ref,
      loadPreview: _previewFor,
      child: ListTile(
      key: Key('pane-${pane.ref}'),
      dense: true,
      selected: _selectedPane?.ref == pane.ref,
      selectedTileColor: AppTheme.accent.withValues(alpha: 0.08),
      contentPadding: const EdgeInsets.only(left: 62, right: 16),
      leading: SizedBox(
        width: 14,
        child: Center(
          child: Container(
            width: 7,
            height: 7,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: status == null
                  ? Colors.transparent
                  : busy
                      ? AppTheme.accent
                      : (status == 'idle' ? AppTheme.green : AppTheme.faint),
            ),
          ),
        ),
      ),
      title: Text(titleText, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13.5)),
      subtitle: Text(
        [pane.ref, if (pane.provider != null) pane.provider!, if (pane.path.isNotEmpty) pane.path].join(' · '),
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(fontSize: 10.5, color: AppTheme.faint),
      ),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          IconButton(
            key: Key('star-${pane.ref}'),
            tooltip: _favorites.contains(pane.ref) ? '取消收藏' : '收藏到主页',
            icon: Icon(
              _favorites.contains(pane.ref) ? Icons.star : Icons.star_border,
              size: 18,
              color: _favorites.contains(pane.ref) ? AppTheme.accent : AppTheme.faint,
            ),
            onPressed: () => _toggleFavorite(pane),
          ),
          pane.hasTranscript
              ? Icon(Icons.chat_bubble_outline, size: 16, color: AppTheme.faint)
              : Icon(Icons.terminal, size: 16, color: AppTheme.faint),
        ],
      ),
        onTap: () => _openPane(pane),
      ),
    );
  }

  // ---- 悬停预览的数据：一次 /api/home，60 秒内复用（和 Web 共用同一个接口）
  Map<String, Map<String, dynamic>>? _previewEntries;
  DateTime? _previewAt;

  Future<PanePreviewData?> _previewFor(String paneRef) async {
    final fresh = _previewEntries != null &&
        _previewAt != null &&
        DateTime.now().difference(_previewAt!) < const Duration(seconds: 60);
    if (!fresh) {
      try {
        final board = await widget.api.homeBoard();
        final entries = <String, Map<String, dynamic>>{};
        for (final item in [
          ...(board['favorites'] as List? ?? const []),
          ...(board['recent'] as List? ?? const []),
        ]) {
          if (item is Map && item['pane'] is String) {
            final key = item['pane'] as String;
            final previous = entries[key];
            final richer = previous != null &&
                (previous['agent_preview'] != null || previous['preview'] != null);
            if (richer && item['agent_preview'] == null && item['preview'] == null) continue;
            entries[key] = Map<String, dynamic>.from(item);
          }
        }
        _previewEntries = entries;
        _previewAt = DateTime.now();
      } catch (_) {
        return null;
      }
    }
    final entry = _previewEntries?[paneRef];
    final source = entry?['agent_preview'] ?? entry?['preview'];
    if (source is! Map) return null;
    final text = (source['text'] as String?)?.trim();
    if (text == null || text.isEmpty) return null;
    return PanePreviewData(
      role: (source['role'] as String?) ?? 'pane',
      text: text,
      time: _previewAgo(source['timestamp']),
    );
  }

  String? _previewAgo(Object? stamp) {
    final seconds = stamp is num ? stamp.toDouble() : double.tryParse('${stamp ?? ''}');
    if (seconds == null || seconds <= 0) return null;
    final ago = DateTime.now().difference(
        DateTime.fromMillisecondsSinceEpoch((seconds * 1000).round()));
    if (ago.inSeconds < 60) return '刚刚';
    if (ago.inMinutes < 60) return '${ago.inMinutes} 分钟前';
    if (ago.inHours < 24) return '${ago.inHours} 小时前';
    return '${ago.inDays} 天前';
  }

  Widget _buildBody() {
    if (_loading && _hosts == null) return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    if (_hosts == null) {
      return ListView(
        padding: const EdgeInsets.all(24),
        children: [
          Text(_error ?? '加载失败', style: TextStyle(color: AppTheme.danger, fontSize: 13)),
          const SizedBox(height: 12),
          OutlinedButton(onPressed: _load, child: const Text('重试')),
        ],
      );
    }
    final hosts = _hosts!;
    if (hosts.isEmpty) {
      return ListView(
        padding: const EdgeInsets.all(24),
        children: [Text('还没有可用的 tmux pane。', style: TextStyle(color: AppTheme.muted))],
      );
    }
    return ListView.builder(
      padding: const EdgeInsets.only(bottom: 12),
      itemCount: hosts.length,
      itemBuilder: (context, index) {
        final host = hosts[index];
        return ExpansionTile(
          key: PageStorageKey('host-${host.id}'),
          initiallyExpanded: true,
          shape: const Border(),
          collapsedShape: const Border(),
          tilePadding: const EdgeInsets.symmetric(horizontal: 16),
          title: Row(
            children: [
              Icon(host.kind == 'local' ? Icons.computer : Icons.cloud_outlined, size: 16, color: AppTheme.faint),
              const SizedBox(width: 8),
              Flexible(child: Text(host.name, style: const TextStyle(fontSize: 14.5, fontWeight: FontWeight.w600))),
              const SizedBox(width: 8),
              Text('${host.panes.length}', style: TextStyle(fontSize: 11.5, color: AppTheme.faint)),
              IconButton(
                key: Key('new-session-${host.id}'),
                tooltip: '新建会话',
                iconSize: 18,
                visualDensity: VisualDensity.compact,
                onPressed: () => _newSession(host),
                icon: const Icon(Icons.add, size: 18),
              ),
            ],
          ),
          children: [
            for (final session in _groupBySession(host.panes))
              ExpansionTile(
                key: PageStorageKey('session-${host.id}-${session.$1}'),
                initiallyExpanded: true,
                tilePadding: const EdgeInsets.only(left: 30, right: 16),
                childrenPadding: EdgeInsets.zero,
                dense: true,
                shape: const Border(),
                collapsedShape: const Border(),
                title: Row(children: [
                  Icon(Icons.folder_outlined, size: 15, color: AppTheme.faint),
                  const SizedBox(width: 7),
                  Flexible(
                    child: Text(session.$1,
                        maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13)),
                  ),
                  const SizedBox(width: 6),
                  Text('${session.$2.length}', style: TextStyle(fontSize: 10.5, color: AppTheme.faint)),
                ]),
                children: [
                  for (final window in _groupByWindow(session.$2))
                    ExpansionTile(
                      key: PageStorageKey('window-${host.id}-${session.$1}-${window.$1}'),
                      initiallyExpanded: true,
                      tilePadding: const EdgeInsets.only(left: 46, right: 16),
                      dense: true,
                      shape: const Border(),
                      collapsedShape: const Border(),
                      title: Text(window.$1,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(fontSize: 12, color: AppTheme.muted)),
                      children: [for (final pane in window.$2) _paneTile(pane)],
                    ),
                ],
              ),
          ],
        );
      },
    );
  }
}

/// Loads a share (unlocking it when needed) and then shows its chat.
class ShareScreen extends StatefulWidget {
  const ShareScreen({super.key, required this.api, required this.slug, this.password = ''});

  final Api api;
  final String slug;
  final String password;

  @override
  State<ShareScreen> createState() => _ShareScreenState();
}

class _ShareScreenState extends State<ShareScreen> {
  ShareMeta? _meta;
  String? _error;
  bool _needsPassword = false;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _load(initialPassword: widget.password);
  }

  Future<void> _load({String initialPassword = ''}) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      if (initialPassword.isNotEmpty && _needsPassword) {
        await widget.api.shareUnlock(widget.slug, initialPassword);
      }
      final meta = await widget.api.shareMeta(widget.slug);
      if (!mounted) return;
      setState(() {
        _meta = meta;
        _needsPassword = false;
        _busy = false;
      });
    } catch (error) {
      if (!mounted) return;
      final needsPassword = error is ApiException && error.isUnauthorized;
      setState(() {
        _needsPassword = needsPassword;
        _error = needsPassword ? '这个分享需要密码' : '$error';
        _busy = false;
      });
    }
  }

  Future<void> _unlock(String password) async {
    try {
      await widget.api.shareUnlock(widget.slug, password);
      await _load();
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = '$error');
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_meta != null) {
      return ChatScreen(source: ShareChatSource(api: widget.api, meta: _meta!));
    }
    return Scaffold(
      appBar: AppBar(title: const Text('共享会话')),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: _needsPassword
              ? _PasswordForm(busy: _busy, error: _error, onSubmit: _unlock)
              : Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (_busy) const CircularProgressIndicator(strokeWidth: 2),
                    if (!_busy)
                      Text(_error ?? '加载失败',
                          textAlign: TextAlign.center, style: TextStyle(color: AppTheme.muted, fontSize: 13)),
                    if (!_busy) ...[
                      const SizedBox(height: 12),
                      OutlinedButton(onPressed: () => _load(), child: const Text('重试')),
                    ],
                  ],
                ),
        ),
      ),
    );
  }
}

class _PasswordForm extends StatefulWidget {
  const _PasswordForm({required this.busy, this.error, required this.onSubmit});

  final bool busy;
  final String? error;
  final void Function(String password) onSubmit;

  @override
  State<_PasswordForm> createState() => _PasswordFormState();
}

class _PasswordFormState extends State<_PasswordForm> {
  final _controller = TextEditingController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text('访问密码', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
        const SizedBox(height: 8),
        TextField(
          key: const Key('share-unlock-password'),
          controller: _controller,
          obscureText: true,
          onSubmitted: widget.onSubmit,
        ),
        if (widget.error != null) ...[
          const SizedBox(height: 10),
          Text(widget.error!, style: const TextStyle(color: Color(0xFFB34B43), fontSize: 12)),
        ],
        const SizedBox(height: 14),
        FilledButton(onPressed: widget.busy ? null : () => widget.onSubmit(_controller.text), child: const Text('打开')),
      ],
    );
  }
}

/// New-session dialog (see _HostsScreenState._newSession). Returns the
/// server's create response on success, null on cancel.
class _NewSessionDialog extends StatefulWidget {
  const _NewSessionDialog({required this.api, required this.host});

  final Api api;
  final Host host;

  @override
  State<_NewSessionDialog> createState() => _NewSessionDialogState();
}

class _NewSessionDialogState extends State<_NewSessionDialog> {
  final TextEditingController _name = TextEditingController();
  late final TextEditingController _path;
  String _status = '';
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    final panes = widget.host.panes;
    _path = TextEditingController(text: panes.isNotEmpty ? panes.first.path : '');
  }

  @override
  void dispose() {
    _name.dispose();
    _path.dispose();
    super.dispose();
  }

  Future<void> _create() async {
    final name = _name.text.trim();
    if (name.isEmpty) {
      setState(() => _status = '请填写会话名称');
      return;
    }
    setState(() {
      _busy = true;
      _status = '正在创建会话…';
    });
    try {
      final result = await widget.api.createSession(
        host: widget.host.id,
        name: name,
        path: _path.text.trim(),
      );
      if (mounted) Navigator.of(context).pop(result);
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _status = '创建失败：$error';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('新建 tmux 会话'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('主机：${widget.host.name}', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
          const SizedBox(height: 12),
          TextField(
            key: const Key('session-name'),
            controller: _name,
            decoration: const InputDecoration(labelText: '会话名称', hintText: 'my-agent'),
          ),
          const SizedBox(height: 10),
          TextField(
            key: const Key('session-path'),
            controller: _path,
            decoration: const InputDecoration(labelText: '工作目录（可选，留空用默认）', hintText: '/home/you/project'),
          ),
          if (_status.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 10),
              child: Text(_status, style: TextStyle(fontSize: 12, color: AppTheme.muted)),
            ),
        ],
      ),
      actions: [
        TextButton(
          key: const Key('session-cancel'),
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('取消'),
        ),
        FilledButton(
          key: const Key('session-create'),
          onPressed: _busy ? null : _create,
          child: const Text('创建'),
        ),
      ],
    );
  }
}
