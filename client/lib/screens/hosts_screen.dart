import 'package:flutter/material.dart';

import 'dart:async';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../models/models.dart';
import '../theme.dart';
import '../widgets/pane_hover_preview.dart';
import 'chat_screen.dart';
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
/// sidebar (web's side-resizer) is not in this cut.
class HostsScreen extends StatefulWidget {
  const HostsScreen({super.key, required this.api, required this.baseUrl});

  final Api api;
  final String baseUrl;

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

  /// Web's narrow breakpoint: at or below 860px the sidebar is an overlay.
  static const double _wideBreakpoint = 861;

  /// Web's --side-w default for the console sidebar.
  static const double _sidebarWidth = 280;

  @override
  void initState() {
    super.initState();
    _load();
    _loadFavorites();
    _stateTimer = Timer.periodic(const Duration(seconds: 9), (_) => _refreshStates());
  }

  /// Web's shell loadFavorites: silent on failure, so a board error never
  /// breaks the tree; the star just reads unstarred until the next load.
  Future<void> _loadFavorites() async {
    try {
      final data = await widget.api.homeFavorites();
      final list = data['favorites'];
      if (list is! List || !mounted) return;
      setState(() => _favorites = list
          .map((item) => item is Map ? '${item['pane'] ?? ''}' : '$item')
          .where((pane) => pane.isNotEmpty)
          .toSet());
    } catch (_) {}
  }

  /// Web's shell toggleStar: DELETE when starred, POST {pane} otherwise,
  /// then reload the set so the row reflects the server, not optimism.
  Future<void> _toggleFavorite(Pane pane) async {
    final starred = _favorites.contains(pane.ref);
    try {
      if (starred) {
        await widget.api.deleteHomeFavorite(pane.ref);
      } else {
        await widget.api.addHomeFavorite(pane.ref);
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
      setState(() {
        _hosts = hosts;
        _error = null;
        _loading = false;
        // Prune a wide-shell selection whose pane went away with the reload.
        final selected = _selectedPane;
        if (selected != null && !hosts.any((host) => host.panes.any((pane) => pane.ref == selected.ref))) {
          _selectedPane = null;
        }
      });
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
    final api = widget.api;
    if (MediaQuery.sizeOf(context).width >= _wideBreakpoint) {
      // Wide shell: select into the detail pane instead of pushing.
      setState(() => _selectedPane = pane);
      return;
    }
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: api is TerminalApi ? api as TerminalApi : null,
        baseUrl: widget.baseUrl,
      ),
    ));
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
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
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
            ? () => setState(() => _selectedPane = null)
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
    ];
  }

  /// Wide shell: fixed sidebar + detail pane, mirroring web's console grid.
  Widget _buildWide() {
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
                      const Text('会话', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
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
                Expanded(child: RefreshIndicator(onRefresh: _load, child: _buildBody())),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                  decoration: BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
                  child: Text(
                    _hosts == null ? '' : '${_hosts!.length} 台主机 · $_paneCount 个 pane',
                    style: TextStyle(fontSize: 11.5, color: AppTheme.faint),
                  ),
                ),
              ],
            ),
          ),
          Expanded(child: _buildDetail()),
        ],
      ),
    );
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
                    Text(pane.label,
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
                      MaterialPageRoute(builder: (_) => TerminalScreen(api: terminalApi, pane: pane))),
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
                onPressed: () => setState(() => _selectedPane = null),
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
      title: Text(pane.label, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13.5)),
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
