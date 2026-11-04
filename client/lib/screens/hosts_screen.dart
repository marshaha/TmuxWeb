import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'dart:async';
import 'dart:convert';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../models/models.dart';
import '../services/pointer_kind.dart';
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
import 'shares_screen.dart';
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

class _HostsScreenState extends State<HostsScreen> with WidgetsBindingObserver {
  List<Host>? _hosts;
  Map<String, String> _states = const {};
  Set<String> _favorites = const {};

  /// Main-area error banner source (web console `#error`): a load
  /// failure, a per-host refresh failure, or a star failure lands
  /// here while the tree keeps its stale content (L5/D1). Cleared by
  /// the next successful load or the banner's dismiss button.
  String? _error;
  Timer? _stateTimer;

  /// Web console 20s poll (index.html:1723): loadPanes() every 20s
  /// while the page is visible. The timer ticks always but only
  /// loads when [_appVisible]; a resume also reloads immediately.
  Timer? _pollTimer;
  bool _appVisible = true;

  /// The pane shown in the detail pane. Null means the detail shows
  /// the home board (wide) or web's empty-selection placeholder
  /// (narrow drawer console, C1). Narrow taps select inline and
  /// close the drawer — they no longer push a chat page.
  Pane? _selectedPane;

  /// Whether the [HostsScreen.initialPaneRef] deep link has been applied.
  bool _initialPaneApplied = false;

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

  /// Web console tree open-state (index.html:339,428; shell.js
  /// loadOpen/saveOpen): the set of expanded node keys under
  /// SharedPreferences 'tmux-tree-open', pruned to still-valid keys
  /// on every load. Empty by default — web starts all-collapsed and
  /// only restores what was saved.
  Set<String> _treeOpen = {};
  bool _treeOpenLoaded = false;

  /// Narrow drawer state (C1, web ≤760px slide-over): false is
  /// translateX(-105%) shut; true slides the drawer in over a
  /// backdrop. The drawer is always built in narrow (off-screen when
  /// shut, like web's transform) so its open state animates.
  bool _drawerOpen = false;
  final _drawerFocusNode = FocusNode();

  /// Host ids currently running a per-host refresh (busy spin, A7).
  final Set<String> _refreshingHosts = {};

  /// Resizer hover/resizing highlight (C3, web ::after 2px line).
  bool _resizerHovered = false;
  bool _resizing = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    final lifecycle = WidgetsBinding.instance.lifecycleState;
    _appVisible = lifecycle == null || lifecycle == AppLifecycleState.resumed;
    // The load runs independently of the prefs read (which may hang
    // without a platform channel, e.g. in tests without mock prefs):
    // _load's prune is a no-op until _treeOpenLoaded, and
    // _loadSidebarPrefs prunes against the loaded hosts once it lands.
    _load();
    unawaited(_loadSidebarPrefs());
    _loadFavorites();
    _loadPins();
    // Account preview-delay wins over the local mirror (settings.js
    // loadPreviewPref); hover reads the mirror from SharedPreferences.
    unawaited(PaneHoverPreview.syncDelayFromAccount(widget.api));
    _stateTimer = Timer.periodic(const Duration(seconds: 9), (_) => _refreshStates());
    _pollTimer = Timer.periodic(const Duration(seconds: 20), (_) {
      if (_appVisible) unawaited(_load());
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final visible = state == AppLifecycleState.resumed;
    if (visible == _appVisible) return;
    _appVisible = visible;
    if (visible) unawaited(_load());
  }

  @override
  void didChangeMetrics() {
    // Web initSidebarResize's window-resize listener: re-clamp the
    // width to the live 60% cap without persisting (C3).
    if (!mounted || _sidebarCollapsed) return;
    final clamped = _clampSidebarWidth(_sidebarWidth);
    if (clamped != _sidebarWidth) setState(() => _sidebarWidth = clamped);
  }

  /// Web initSidebarResize's clamp: max(200, min(640, 60% of window,
  /// width)) — the 60% cap also applies at load time (C3), where the
  /// old code clamped only to 200–640. On a very narrow window the
  /// 60% cap drops below the 200 floor and web's outer Math.max
  /// still yields 200.
  double _clampSidebarWidth(double width) {
    final views = WidgetsBinding.instance.platformDispatcher.views;
    var windowWidth = 1400.0;
    if (views.isNotEmpty) {
      final view = views.first;
      if (view.devicePixelRatio > 0) windowWidth = view.physicalSize.width / view.devicePixelRatio;
    }
    final cap = [640.0, windowWidth * 0.6].reduce((a, b) => a < b ? a : b);
    if (cap < 200) return 200;
    return width.clamp(200, cap);
  }

  Future<void> _loadSidebarPrefs() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      Set<String> treeOpen = {};
      try {
        final raw = prefs.getString('tmux-tree-open');
        if (raw != null && raw.isNotEmpty) {
          final decoded = jsonDecode(raw);
          if (decoded is List) {
            treeOpen = decoded.whereType<String>().toSet();
          }
        }
      } catch (_) {}
      if (!mounted) return;
      setState(() {
        _sidebarWidth = _clampSidebarWidth(prefs.getDouble('tmux-side-w') ?? _sidebarDefaultWidth);
        _sidebarCollapsed = prefs.getBool('tmux-sidebar-collapsed') ?? false;
        _treeOpen = treeOpen;
        _treeOpenLoaded = true;
      });
      _sidebarMemoryOn = prefs.getString('tmux-sidebar-memory') == 'pane';
      _pruneTreeOpen();
    } catch (_) {}
  }

  /// Web loadPanes' prune (index.html:428): keep only keys that still
  /// name a host, a session, or a window group. Runs on every load
  /// and after the prefs land; a no-op until the prefs have loaded
  /// so a load racing the prefs cannot wipe a restored tree.
  void _pruneTreeOpen() {
    if (!_treeOpenLoaded) return;
    final valid = _treeKeys();
    final pruned = _treeOpen.intersection(valid);
    if (pruned.length != _treeOpen.length) {
      if (mounted) {
        setState(() => _treeOpen = pruned);
      } else {
        _treeOpen = pruned;
      }
      unawaited(_saveTreeOpen());
    }
  }

  /// Every valid tree key for the current hosts, in web's shape:
  /// host id, `host/session`, `host/session/window:window_name`.
  Set<String> _treeKeys() {
    final keys = <String>{};
    for (final host in _hosts ?? const <Host>[]) {
      keys.add(host.id);
      for (final session in _groupBySession(host.panes)) {
        keys.add('${host.id}/${session.$1}');
        for (final window in _groupByWindow(session.$2)) {
          keys.add('${host.id}/${session.$1}/${window.$1}');
        }
      }
    }
    return keys;
  }

  Future<void> _saveTreeOpen() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString('tmux-tree-open', jsonEncode(_treeOpen.toList()));
    } catch (_) {}
  }

  void _setTreeOpen(String key, bool open) {
    setState(() {
      if (open) {
        _treeOpen.add(key);
      } else {
        _treeOpen.remove(key);
      }
    });
    unawaited(_saveTreeOpen());
  }

  /// Tree-tools expand-all / collapse-all (A1, index.html:295,1687).
  void _expandAllTree() {
    setState(() => _treeOpen = _treeKeys());
    unawaited(_saveTreeOpen());
  }

  void _collapseAllTree() {
    setState(() => _treeOpen = {});
    unawaited(_saveTreeOpen());
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

  /// Web console tree star (index.html:439 toggleFavorite): SILENT
  /// on success, error → the main error banner (D1). The old
  /// success SnackBar (已收藏到主页/已取消收藏) is gone.
  Future<void> _toggleFavoriteRef(String ref) async {
    final starred = _favorites.contains(ref);
    try {
      if (starred) {
        await widget.api.deleteHomeFavorite(ref);
      } else {
        await widget.api.addHomeFavorite(ref);
      }
      await _loadFavorites();
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = '$error');
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _stateTimer?.cancel();
    _pollTimer?.cancel();
    _drawerFocusNode.dispose();
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
    _touchRecent(target);
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

  /// Web console loadPanes (index.html:428): a load never replaces
  /// the tree with a spinner or an error page — on failure the stale
  /// tree stays and the error goes to the main banner (L5/L6). The
  /// header/global refresh passes [forceRefresh] so it calls
  /// hosts(refresh: true) (A8); polls and pull-to-refresh do not.
  Future<void> _load({bool forceRefresh = false}) async {
    try {
      final hosts = await widget.api.hosts(refresh: forceRefresh);
      if (!mounted) return;
      Pane? deepLinked;
      setState(() {
        _hosts = hosts;
        _error = null;
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
        _touchRecent(deepLinkedPane);
        unawaited(_applySidebarViewForPane(deepLinkedPane));
      }
      _pruneTreeOpen();
      unawaited(_refreshStates());
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = '$error');
    }
  }

  int get _paneCount => (_hosts ?? const <Host>[]).fold<int>(0, (sum, host) => sum + host.panes.length);

  /// Every pane ref currently in the tree, for the recent block's
  /// knownPaneRefs filter (E) — the same set _buildBody renders.
  Set<String> get _treePaneRefs => {
        for (final host in _hosts ?? const <Host>[])
          for (final pane in host.panes) pane.ref,
      };

  /// Web console selectPane's touch (index.html:1001, TmuxRecents
  /// touch): EVERY selection touches the recent entry — tree taps,
  /// recent picks, and deep links alike — then markActive follows
  /// through the block's activePaneRef. Silent on failure, like web.
  void _touchRecent(Pane pane) {
    unawaited(widget.api.touchHomeRecent(pane.ref).catchError((_) {}));
  }

  /// Web console refreshHost (index.html:416, shell.js:275-293):
  /// busy spin while refreshing, toast on success, error → the main
  /// banner. The agreed Api contract exposes hosts(refresh:) without
  /// a per-host filter, so the button re-probes all hosts through it
  /// rather than a host-scoped request web would send.
  Future<void> _refreshHost(Host host) async {
    if (_refreshingHosts.contains(host.id)) return;
    setState(() => _refreshingHosts.add(host.id));
    try {
      await _load(forceRefresh: true);
      if (!mounted) return;
      if (_error == null) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('已刷新')));
      }
    } finally {
      if (mounted) setState(() => _refreshingHosts.remove(host.id));
    }
  }

  void _openPane(Pane pane) {
    // Wide and narrow alike: select into the detail pane. Narrow
    // additionally closes the drawer (web: selecting a pane closes
    // the slide-over, index.html:1687 pane-list onclick). The old
    // narrow push-a-chat-page flow is gone with the C1 restructure.
    final narrow = MediaQuery.sizeOf(context).width < _wideBreakpoint;
    setState(() {
      _selectedPane = pane;
      if (narrow) _drawerOpen = false;
    });
    _touchRecent(pane);
    unawaited(_applySidebarViewForPane(pane));
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
  /// Narrow console (C1, web ≤760px): a detail-first body — the
  /// selected pane's conversation, or web's empty-selection
  /// placeholder — with the sidebar as a slide-over drawer
  /// (min(86vw, 330px), shut = translateX(-105%)) plus a backdrop.
  /// The topbar menu button opens it; Escape closes it; a backdrop
  /// tap closes it; selecting a pane closes it (in _openPane).
  /// Mobile-composing hides sidebar+backdrop on web; here that state
  /// cannot coexist with an open drawer — selecting a pane already
  /// closed it, and opening the drawer unfocuses the composer — so
  /// no extra composing branch is needed.
  Widget _buildNarrow() {
    final screenWidth = MediaQuery.sizeOf(context).width;
    final drawerWidth = [screenWidth * 0.86, 330.0].reduce((a, b) => a < b ? a : b);
    return Focus(
      focusNode: _drawerFocusNode,
      // Autofocus so Escape reaches this handler even before any
      // control takes focus; focus later moves into the detail chat,
      // and the event still bubbles up to this ancestor handler.
      autofocus: true,
      onKeyEvent: (node, event) {
        if (event is KeyDownEvent && event.logicalKey == LogicalKeyboardKey.escape && _drawerOpen) {
          setState(() => _drawerOpen = false);
          return KeyEventResult.handled;
        }
        return KeyEventResult.ignored;
      },
      child: Scaffold(
        appBar: AppBar(
          leading: IconButton(
            key: const Key('narrow-menu'),
            tooltip: '展开侧边栏',
            icon: const Icon(Icons.menu, size: 20),
            onPressed: () {
              // Unfocus dismisses the mobile keyboard, but focus must
              // land somewhere inside this Focus subtree or Escape
              // has no node to bubble from — park it on the drawer.
              FocusScope.of(context).unfocus();
              _drawerFocusNode.requestFocus();
              setState(() => _drawerOpen = true);
            },
          ),
          title: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text('Tmux Chat'),
              Text('本机实时会话', style: TextStyle(fontSize: 11, fontWeight: FontWeight.w400, color: AppTheme.faint)),
            ],
          ),
          actions: _actions(wide: false),
        ),
        body: Stack(
          children: [
            _buildDetailSection(narrow: true),
            if (_drawerOpen)
              Positioned.fill(
                child: GestureDetector(
                  key: const Key('drawer-backdrop'),
                  onTap: () => setState(() => _drawerOpen = false),
                  child: Container(color: const Color(0x59141412)),
                ),
              ),
            AnimatedPositioned(
              duration: const Duration(milliseconds: 180),
              left: _drawerOpen ? 0 : -drawerWidth * 1.05,
              top: 0,
              bottom: 0,
              child: Container(
                key: const Key('narrow-drawer'),
                width: drawerWidth,
                decoration: BoxDecoration(
                  color: AppTheme.sidebar,
                  boxShadow: const [BoxShadow(color: Color(0x2E000000), blurRadius: 50, offset: Offset(18, 0))],
                ),
                child: _buildSidebarContent(),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _openSettings() async {
    await Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => SettingsScreen(api: widget.api, baseUrl: widget.baseUrl),
    ));
    await _load();
  }

  /// Shared header actions. Wide mode differs in one place: the board
  /// button shows the board in the detail pane instead of pushing a page.
  /// With [brandRow] the buttons are the sidebar brand row (web
  /// .brand-actions): files-toggle, shares-manage, and collapse join
  /// the core set, each in an exact 30px box (40px on coarse
  /// pointers, B4); settings is NOT here — it lives in the footer
  /// (B2). The header refresh is a FORCED refresh (A8).
  /// Brand buttons are InkWell + SizedBox, not IconButton: IconButton
  /// floors at 48px in this SDK even with tight constraints, so it
  /// cannot render web's 30/40px boxes.
  List<Widget> _actions({required bool wide, bool brandRow = false}) {
    final density = wide ? VisualDensity.compact : null;
    final api = widget.api;
    final cookie = api is TerminalApi
        ? (api as TerminalApi).wsHeaders['Cookie'] ?? ''
        : '';
    Widget brandButton({
      Key? key,
      required String tooltip,
      required IconData icon,
      required VoidCallback onPressed,
      Color? color,
    }) {
      if (brandRow) {
        final box = hasCoarsePointer ? 40.0 : 30.0;
        return Tooltip(
          message: tooltip,
          child: SizedBox(
            width: box,
            height: box,
            child: Semantics(
              button: true,
              enabled: true,
              child: InkWell(
                key: key,
                borderRadius: BorderRadius.circular(10),
                onTap: onPressed,
                child: Icon(icon, size: 18, color: color),
              ),
            ),
          ),
        );
      }
      return IconButton(
        key: key,
        tooltip: tooltip,
        visualDensity: density,
        onPressed: onPressed,
        icon: Icon(icon, size: 20, color: color),
      );
    }

    return [
      brandButton(
        key: const Key('grid'),
        tooltip: '多窗口',
        icon: Icons.grid_view_outlined,
        onPressed: () => Navigator.of(context).push(
            MaterialPageRoute(builder: (_) => GridScreen(api: widget.api, baseUrl: widget.baseUrl))),
      ),
      brandButton(
        key: const Key('home-board'),
        tooltip: '主页板',
        icon: Icons.dashboard_outlined,
        color: wide && _selectedPane == null ? AppTheme.accent : null,
        onPressed: wide
            ? () => setState(() {
                  _selectedPane = null;
                  _filesMode = false;
                })
            : () => Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => HomeBoardScreen(api: widget.api, baseUrl: widget.baseUrl))),
      ),
      // Web's console `#files-toggle`: the file panel replaces the
      // tree while open (see _buildSidebarContent).
      if (brandRow)
        brandButton(
          key: const Key('sidebar-files-toggle'),
          tooltip: _filesMode ? '隐藏文件面板' : '显示文件面板',
          icon: Icons.folder_outlined,
          color: _filesMode ? AppTheme.accent : null,
          onPressed: _toggleFilesPanel,
        ),
      brandButton(
        key: const Key('hosts-admin'),
        tooltip: '服务器管理',
        icon: Icons.dns_outlined,
        onPressed: () async {
          await Navigator.of(context).push(MaterialPageRoute(
            builder: (_) => HostsAdminScreen(api: widget.api),
          ));
          await _load();
        },
      ),
      // Web brand action "管理分享" (index.html:294, B3): the
      // SharesScreen management page. The open-share-by-slug dialog
      // below stays as-is.
      if (brandRow)
        brandButton(
          key: const Key('shares-manage'),
          tooltip: '管理分享',
          icon: Icons.open_in_new,
          onPressed: () => Navigator.of(context).push(MaterialPageRoute(
            builder: (_) => SharesScreen(api: widget.api, baseUrl: widget.baseUrl),
          )),
        ),
      brandButton(
        tooltip: '打开分享链接',
        icon: Icons.link,
        onPressed: _openShare,
      ),
      brandButton(
        key: const Key('header-refresh'),
        tooltip: '刷新',
        icon: Icons.refresh,
        onPressed: () => _load(forceRefresh: true),
      ),
      if (brandRow)
        brandButton(
          key: const Key('sidebar-collapse'),
          tooltip: '折叠侧边栏',
          icon: Icons.chevron_left,
          onPressed: () {
            setState(() => _sidebarCollapsed = true);
            _saveSidebarPrefs();
          },
        ),
      // Web's plugins-ui.js buttons, appended to the console's action
      // row; renders nothing when the server reports no buttons.
      PluginButtons(
          api: api, baseUrl: widget.baseUrl, cookieHeader: cookie),
    ];
  }

  /// The sidebar brand header (B1/B4, web/index.html:294,93-110):
  /// `Tmux Chat` + `本机实时会话`. Fine pointers get a wrapping icon
  /// row; coarse pointers get one swipeable row (40px icons) with a
  /// fade-edge hint, like web's .brand-actions coarse rules.
  Widget _brandHeader() {
    final text = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text('Tmux Chat', style: TextStyle(fontSize: 20, fontFamily: 'Georgia')),
        Text('本机实时会话', style: TextStyle(fontSize: 11, color: AppTheme.faint)),
      ],
    );
    final actions = _actions(wide: true, brandRow: true);
    if (hasCoarsePointer) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          text,
          const SizedBox(height: 9),
          SizedBox(
            height: 40,
            child: Stack(
              children: [
                SingleChildScrollView(
                  scrollDirection: Axis.horizontal,
                  child: Row(children: actions),
                ),
                Positioned(
                  right: 0,
                  top: 0,
                  bottom: 0,
                  child: IgnorePointer(
                    child: Container(
                      width: 22,
                      decoration: BoxDecoration(
                        gradient: LinearGradient(
                          colors: [AppTheme.sidebar.withValues(alpha: 0), AppTheme.sidebar],
                        ),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      );
    }
    // The Wrap MUST sit in an Expanded: inside a Row a bare Wrap
    // gets unbounded width, never wraps, and overflows past the
    // sidebar under the detail — which then swallows its taps.
    return Row(
      children: [
        Expanded(child: text),
        Expanded(child: Wrap(children: actions)),
      ],
    );
  }

  /// The sidebar column shared by the wide shell and the narrow
  /// drawer: brand header, tree-tools row, tree/file panel, recent
  /// block, footer.
  Widget _buildSidebarContent() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 16, 8, 8),
          child: _brandHeader(),
        ),
        // Tree-tools row (A1, index.html:295): label + expand-all /
        // collapse-all, above the pane list.
        Container(
          padding: const EdgeInsets.fromLTRB(10, 2, 10, 2),
          child: Row(
            children: [
              Expanded(
                child: Text('会话',
                    style: TextStyle(fontSize: 11, letterSpacing: 0.06, color: AppTheme.faint)),
              ),
              IconButton(
                key: const Key('tree-expand'),
                tooltip: '展开全部',
                padding: EdgeInsets.zero,
                constraints: const BoxConstraints.tightFor(width: 24, height: 24),
                iconSize: 14,
                onPressed: _expandAllTree,
                icon: const Icon(Icons.unfold_more, size: 14),
              ),
              IconButton(
                key: const Key('tree-collapse'),
                tooltip: '折叠全部',
                padding: EdgeInsets.zero,
                constraints: const BoxConstraints.tightFor(width: 24, height: 24),
                iconSize: 14,
                onPressed: _collapseAllTree,
                icon: const Icon(Icons.unfold_less, size: 14),
              ),
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
                  hostLabel: _hostLabelForSelectedPane(),
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
            // _toggleFavoriteRef is silent on success (D1) and
            // banners errors; the block re-reads it on refresh.
            await _toggleFavoriteRef(ref);
          },
          fixedBottom: true,
          hideWhenFiles: true,
          isFilesPanelOpen: (_filesMode && _selectedPane != null),
          activePaneRef: _selectedPane?.ref,
          knownPaneRefs: _treePaneRefs,
          sidebarHeight: MediaQuery.of(context).size.height,
          onClearUnpinned: (_) => widget.api.clearUnpinnedRecents(),
          onRenameFavoriteTitle: (ref, label) => widget.api.renameHomeFavorite(ref, label),
          loadPreview: _previewFor,
        ),
        _buildSidebarFooter(),
      ],
    );
  }

  /// The owning host's name for the file panel's host chip (E): the
  /// Host whose panes contain the selected pane. Null falls back to
  /// the panel's web remote/localhost rule.
  String? _hostLabelForSelectedPane() {
    final selected = _selectedPane;
    if (selected == null) return null;
    for (final host in _hosts ?? const <Host>[]) {
      if (host.panes.any((pane) => pane.ref == selected.ref)) return host.name;
    }
    return null;
  }

  /// Sidebar footer (B2, web/index.html:297): pane count + `设置`
  /// chip button. Settings moved here from the header row.
  Widget _buildSidebarFooter() {
    return Container(
      padding: EdgeInsets.fromLTRB(16, 10, 16, 10 + MediaQuery.of(context).padding.bottom),
      decoration: BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
      child: Row(
        children: [
          Expanded(
            child: Text(
              '${_hosts?.length ?? 0} 台主机 · $_paneCount 个 pane',
              style: TextStyle(fontSize: 11.5, color: AppTheme.faint),
            ),
          ),
          OutlinedButton.icon(
            key: const Key('footer-settings'),
            style: OutlinedButton.styleFrom(
              padding: const EdgeInsets.symmetric(horizontal: 9),
              minimumSize: const Size(0, 23),
              tapTargetSize: MaterialTapTargetSize.shrinkWrap,
              textStyle: const TextStyle(fontSize: 11),
            ),
            onPressed: _openSettings,
            icon: const Icon(Icons.settings_outlined, size: 14),
            label: const Text('设置'),
          ),
        ],
      ),
    );
  }

  /// Main-area error banner (L5, web console `#error`): shows above
  /// the detail while the tree keeps stale content; retry re-runs
  /// the load, dismiss clears it.
  Widget _mainErrorBanner() {
    return Container(
      key: const Key('console-error-banner'),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      decoration: BoxDecoration(
        color: AppTheme.danger.withValues(alpha: 0.12),
        border: Border(bottom: BorderSide(color: AppTheme.danger.withValues(alpha: 0.3))),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(_error!,
                maxLines: 2, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 12, color: AppTheme.danger)),
          ),
          TextButton(onPressed: _load, child: const Text('重试')),
          IconButton(
            tooltip: '关闭',
            iconSize: 16,
            visualDensity: VisualDensity.compact,
            onPressed: () => setState(() => _error = null),
            icon: const Icon(Icons.close, size: 16),
          ),
        ],
      ),
    );
  }

  /// Detail section: the error banner (when set) above the detail.
  Widget _buildDetailSection({bool narrow = false, bool showExpandMenu = false}) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (_error != null) _mainErrorBanner(),
        Expanded(child: _buildDetail(narrow: narrow, showExpandMenu: showExpandMenu)),
      ],
    );
  }

  /// Wide shell: resizable + collapsible sidebar + detail pane,
  /// mirroring web's console grid. The 861px breakpoint is unchanged.
  /// Collapsed (C2): no floating overlay button — the sidebar column
  /// goes to 0 width and the detail's own header carries the menu
  /// button (web's topbar .sidebar-open).
  Widget _buildWide() {
    if (_sidebarCollapsed) {
      return Scaffold(body: _buildDetailSection(showExpandMenu: true));
    }
    return Scaffold(
      body: Row(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            key: const Key('console-sidebar'),
            width: _sidebarWidth,
            decoration: BoxDecoration(
              color: AppTheme.sidebar,
              border: Border(right: BorderSide(color: AppTheme.line)),
            ),
            child: _buildSidebarContent(),
          ),
          _sidebarResizer(),
          Expanded(child: _buildDetailSection()),
        ],
      ),
    );
  }

  /// Drag handle between sidebar and detail (web shell.js side-resizer):
  /// drag to resize (200–640px, capped at 60% of the window), double-click
  /// to reset to 280. Width persists on drag end and on reset.
  /// C3: the 60% cap now comes from [_clampSidebarWidth] (shared with
  /// load-time and window-resize clamping), and the 8px handle shows
  /// web's ::after 2px highlight line on hover/resizing, with the
  /// exact resizer tooltip (B5).
  Widget _sidebarResizer() {
    return Tooltip(
      message: '拖动调整侧边栏宽度（双击恢复默认）',
      child: MouseRegion(
        cursor: SystemMouseCursors.resizeColumn,
        onEnter: (_) => setState(() => _resizerHovered = true),
        onExit: (_) => setState(() => _resizerHovered = false),
        child: GestureDetector(
          key: const Key('sidebar-resizer'),
          // Record on pointer-down, not drag-start: drag-start fires
          // after the touch slop is consumed, which would eat ~20px of
          // every resize (web reads pointerdown the same way).
          onHorizontalDragDown: (details) {
            _dragStartX = details.globalPosition.dx;
            _dragStartWidth = _sidebarWidth;
            setState(() => _resizing = true);
          },
          onHorizontalDragUpdate: (details) {
            final start = _dragStartWidth;
            if (start == null) return;
            setState(() => _sidebarWidth =
                _clampSidebarWidth(start + details.globalPosition.dx - _dragStartX));
          },
          onHorizontalDragEnd: (_) {
            setState(() => _resizing = false);
            _saveSidebarPrefs();
          },
          onHorizontalDragCancel: () => setState(() => _resizing = false),
          onDoubleTap: () {
            setState(() => _sidebarWidth = _sidebarDefaultWidth);
            _saveSidebarPrefs();
          },
          child: Container(
            width: 8,
            color: Colors.transparent,
            child: Align(
              alignment: Alignment.centerRight,
              child: Container(
                width: 2,
                color: (_resizerHovered || _resizing) ? const Color(0xFFB9B4A8) : Colors.transparent,
              ),
            ),
          ),
        ),
      ),
    );
  }

  /// Collapsed-sidebar expand control (C2, web topbar #menu): the
  /// menu button that re-opens the sidebar. Rendered as the first
  /// item of the detail header (or of the collapsed top row when no
  /// pane is selected), never as a floating overlay.
  Widget _sidebarExpandButton() {
    return IconButton(
      key: const Key('sidebar-expand'),
      tooltip: '展开侧边栏',
      icon: const Icon(Icons.menu, size: 20),
      onPressed: () {
        setState(() => _sidebarCollapsed = false);
        _saveSidebarPrefs();
      },
    );
  }

  /// The recent block's data: pinned rows first (their labels feed
  /// nameForPane), then recent rows, from GET /api/home/recent.
  /// The console mount reads with limit 12 (E, web's ?limit=12).
  Future<RecentBlockData> _loadRecentBlock() async {
    final data = await widget.api.pins(limit: 12);
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
  Widget _buildDetail({bool narrow = false, bool showExpandMenu = false}) {
    final pane = _selectedPane;
    if (pane == null) {
      if (narrow) {
        // Web's narrow empty-selection state (index.html #empty):
        // the conversation area shows the "select a session"
        // placeholder, not the home board.
        return const Center(
          child: Padding(
            padding: EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('选择一个会话', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
                SizedBox(height: 6),
                Text('选择一个 tmux 会话，实时查看内容，并在下方继续输入',
                    textAlign: TextAlign.center, style: TextStyle(fontSize: 13)),
              ],
            ),
          ),
        );
      }
      if (showExpandMenu) {
        // Collapsed wide shell with no pane: the topbar row carries
        // the expand button above the board (C2).
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
              decoration: BoxDecoration(border: Border(bottom: BorderSide(color: AppTheme.line))),
              child: Row(
                children: [
                  _sidebarExpandButton(),
                  const Text('Tmux Chat', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
                ],
              ),
            ),
            Expanded(child: HomeBoardScreen(api: widget.api, baseUrl: widget.baseUrl)),
          ],
        );
      }
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
              if (showExpandMenu) _sidebarExpandButton(),
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

  /// host → session → window grouping, in SERVER order (A3): web
  /// groups with insertion-order Maps (index.html:720), so no
  /// alphabetical key sorting here — a sort would reorder the tree
  /// away from what the server returned.
  List<(String, List<Pane>)> _groupBySession(List<Pane> panes) {
    final map = <String, List<Pane>>{};
    for (final pane in panes) {
      final key = pane.session.isEmpty ? 'default' : pane.session;
      map.putIfAbsent(key, () => []).add(pane);
    }
    return [for (final entry in map.entries) (entry.key, entry.value)];
  }

  /// Window grouping key (A5): `window:window_name` (index.html:720),
  /// NOT pane.index — several panes in one window must be ONE group.
  /// The record's $1 is the key; the label comes from [_windowLabel].
  List<(String, List<Pane>)> _groupByWindow(List<Pane> panes) {
    final map = <String, List<Pane>>{};
    for (final pane in panes) {
      final key = '${pane.window}:${pane.windowName}';
      map.putIfAbsent(key, () => []).add(pane);
    }
    return [for (final entry in map.entries) (entry.key, entry.value)];
  }

  /// Web window label (index.html:720): `{window}` plus `: {name}`
  /// when the window has a name. Both empty degrades to 'window'
  /// (web would render an empty label there).
  String _windowLabel(Pane pane) {
    final label = '${pane.window}${pane.windowName.isNotEmpty ? ': ${pane.windowName}' : ''}';
    return label.isEmpty ? 'window' : label;
  }

  /// Web console pane row (index.html:720, A6), rebuilt as a custom
  /// row — a ListTile cannot carry web's three lines:
  /// row 1: active dot (green iff pane.active) · `#N` or `—` ·
  /// `pane {index} · {command}` · star · state dot (idle green /
  /// busy #d89a3c amber / unknown #cfccc4 grey);
  /// row 2: title line; row 3: path line.
  /// Gone: the ref/provider subtitle (web's console row never shows
  /// it) and the trailing chat/terminal icon (no web counterpart).
  Widget _paneTile(Pane pane) {
    final status = _states[pane.ref];
    final displayName = nameForPane(
        paneRef: pane.ref, pinLabels: _pinLabels, favoriteTitles: _favoriteTitles, fallback: pane.label);
    final starred = _favorites.contains(pane.ref);
    final selected = _selectedPane?.ref == pane.ref;
    final stateColor = switch (status) {
      'idle' => AppTheme.green,
      'busy' => const Color(0xFFD89A3C),
      _ => const Color(0xFFCFCCC4),
    };
    return PaneHoverPreview(
      paneRef: pane.ref,
      loadPreview: _previewFor,
      child: Container(
        key: Key('pane-${pane.ref}'),
        margin: const EdgeInsets.symmetric(vertical: 1),
        decoration: selected
            ? BoxDecoration(
                color: AppTheme.accent.withValues(alpha: 0.08),
                borderRadius: BorderRadius.circular(8),
              )
            : null,
        child: InkWell(
          borderRadius: BorderRadius.circular(8),
          onTap: () => _openPane(pane),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(62, 6, 8, 6),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Container(
                      width: 7,
                      height: 7,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: pane.active ? AppTheme.green : AppTheme.faint,
                      ),
                    ),
                    const SizedBox(width: 8),
                    SizedBox(
                      width: 26,
                      child: Text(
                        pane.numberLabel.isEmpty ? '—' : pane.numberLabel,
                        style: TextStyle(fontSize: 10.5, color: AppTheme.faint, fontFamily: 'monospace'),
                      ),
                    ),
                    Expanded(
                      child: Text(
                        'pane ${pane.index} · ${pane.command}',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 13.5, fontWeight: FontWeight.w500),
                      ),
                    ),
                    SizedBox(
                      width: 28,
                      height: 28,
                      child: IconButton(
                        key: Key('star-${pane.ref}'),
                        tooltip: starred ? '取消收藏' : '收藏到主页',
                        padding: EdgeInsets.zero,
                        icon: Icon(
                          starred ? Icons.star : Icons.star_border,
                          size: 18,
                          color: starred ? AppTheme.accent : AppTheme.faint,
                        ),
                        onPressed: () => _toggleFavorite(pane),
                      ),
                    ),
                    const SizedBox(width: 2),
                    Container(
                      key: Key('pane-state-${pane.ref}'),
                      width: 6,
                      height: 6,
                      decoration: BoxDecoration(shape: BoxShape.circle, color: stateColor),
                    ),
                  ],
                ),
                Padding(
                  padding: const EdgeInsets.only(left: 15, top: 4),
                  child: Text(displayName,
                      maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 12, color: AppTheme.muted)),
                ),
                Padding(
                  padding: const EdgeInsets.only(left: 15, top: 2),
                  child: Text(pane.path,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: 11, color: AppTheme.faint, fontFamily: 'monospace')),
                ),
              ],
            ),
          ),
        ),
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

  /// The console tree. Web console states (L4/L5/L6):
  /// - no hosts yet (or none at all): an EMPTY pane-list, no message
  ///   and no sidebar spinner — the count footer already reads 0;
  /// - load failure: the stale tree stays (the error lives in the
  ///   main-area banner, not here), so this never renders an error
  ///   page or a retry button.
  /// Expansion is controlled, not initiallyExpanded + PageStorageKey:
  /// each tile's key carries its open state from [_treeOpen] so a
  /// toggle, expand-all, or collapse-all re-renders it, and the set
  /// persists under 'tmux-tree-open' (A2).
  Widget _buildBody() {
    final hosts = _hosts ?? const <Host>[];
    if (hosts.isEmpty) {
      return ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.only(bottom: 12),
      );
    }
    return ListView.builder(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.only(bottom: 12),
      itemCount: hosts.length,
      itemBuilder: (context, index) {
        final host = hosts[index];
        final hostKey = host.id;
        final hostOpen = _treeOpen.contains(hostKey);
        final refreshing = _refreshingHosts.contains(host.id);
        return ExpansionTile(
          key: ValueKey('host-$hostKey-${hostOpen ? 'open' : 'shut'}'),
          initiallyExpanded: hostOpen,
          onExpansionChanged: (open) => _setTreeOpen(hostKey, open),
          shape: const Border(),
          collapsedShape: const Border(),
          tilePadding: const EdgeInsets.symmetric(horizontal: 16),
          title: Row(
            children: [
              Icon(host.kind == 'local' ? Icons.computer : Icons.cloud_outlined, size: 16, color: AppTheme.faint),
              const SizedBox(width: 8),
              Flexible(child: Text(host.name, style: const TextStyle(fontSize: 14.5, fontWeight: FontWeight.w600))),
              const SizedBox(width: 8),
              // Web's .host-status: always green — web never updates
              // it from live data, so static green is correct (A7).
              Container(
                width: 7,
                height: 7,
                decoration: BoxDecoration(shape: BoxShape.circle, color: AppTheme.green),
              ),
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
              IconButton(
                key: Key('host-refresh-${host.id}'),
                tooltip: '刷新这台主机的 pane 列表',
                iconSize: 18,
                visualDensity: VisualDensity.compact,
                onPressed: refreshing ? null : () => _refreshHost(host),
                icon: refreshing
                    ? const SizedBox(
                        width: 14,
                        height: 14,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.refresh, size: 18),
              ),
            ],
          ),
          children: [
            for (final session in _groupBySession(host.panes))
              Builder(builder: (context) {
                final windows = _groupByWindow(session.$2);
                final sessionKey = '${host.id}/${session.$1}';
                final sessionOpen = _treeOpen.contains(sessionKey);
                return ExpansionTile(
                  key: ValueKey('session-$sessionKey-${sessionOpen ? 'open' : 'shut'}'),
                  initiallyExpanded: sessionOpen,
                  onExpansionChanged: (open) => _setTreeOpen(sessionKey, open),
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
                    // Web session count is the number of WINDOWS
                    // (`${windows.size} windows`, index.html:720),
                    // not the pane count (A4).
                    Text('${windows.length} windows', style: TextStyle(fontSize: 10.5, color: AppTheme.faint)),
                  ]),
                  children: [
                    for (final window in windows)
                      Builder(builder: (context) {
                        final windowKey = '${host.id}/${session.$1}/${window.$1}';
                        final windowOpen = _treeOpen.contains(windowKey);
                        return ExpansionTile(
                          key: ValueKey('window-$windowKey-${windowOpen ? 'open' : 'shut'}'),
                          initiallyExpanded: windowOpen,
                          onExpansionChanged: (open) => _setTreeOpen(windowKey, open),
                          tilePadding: const EdgeInsets.only(left: 46, right: 16),
                          dense: true,
                          shape: const Border(),
                          collapsedShape: const Border(),
                          title: Row(children: [
                            Flexible(
                              child: Text(_windowLabel(window.$2.first),
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(fontSize: 12, color: AppTheme.muted)),
                            ),
                            const SizedBox(width: 6),
                            Text('${window.$2.length}', style: TextStyle(fontSize: 10.5, color: AppTheme.faint)),
                          ]),
                          children: [for (final pane in window.$2) _paneTile(pane)],
                        );
                      }),
                  ],
                );
              }),
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
