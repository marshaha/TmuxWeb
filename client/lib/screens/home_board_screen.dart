/// Home board: the Flutter equivalent of web/home.html.
///
/// Web behaviour, followed here rather than approximated:
/// - First paint comes from the cached board (slimmed like web's
///   slimBoard: favorites 40, recent 4, cards 40, preview/body text 600
///   chars), marked stale ("显示缓存，正在更新…"); the live board replaces
///   it and rewrites the cache. A failed live load keeps the cached board
///   and only shows a snackbar, like web's toast on load failure.
/// - Live reload every 15s, skipped while the app is backgrounded or this
///   screen is not the current route (web skips when document.hidden);
///   a still-pending favorite reloads after 2.5s, unknown stats (0 panes)
///   retry after 4s, both one-shot like web's timers.
/// - A board card never navigates directly: tapping it opens the preview
///   panel (web's hover preview), and the panel holds the way in
///   ("打开对话"). The panel shows the last agent turn, not the card's
///   last message, with the offline/cached origin and the age label.
/// - Cards are created by the MCP publish_home_card tool, not by a UI form,
///   exactly like web: there is no "new card" button here.
///
/// Full preview, kept distinct from the console chat, like web:
/// - The preview panel (web's hover preview) has three ways out:
///   "完整预览", "聊天", "关闭". "聊天" opens the console chat screen.
/// - "完整预览" is the preview page itself, and it can talk: on a wide
///   screen (web's PREVIEW_WIDE = 1100) it opens as a drawer on the right
///   side of the board, with a backdrop, a header (title, favorite star,
///   terminal, console button, close) and the full transcript + composer
///   below; on a narrow screen it navigates to the full preview page
///   (PanePreviewScreen), exactly like web's /preview?pane= page.
/// - The drawer header merges web's drawer chrome (title, console, close)
///   and the preview page's header (favorite, terminal) into one row, so
///   the panel does not carry two stacked headers.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../models/home_board.dart';
import '../models/models.dart';
import '../theme.dart';
import '../widgets/favorite_star_button.dart';
import 'chat_screen.dart';
import 'home_search_screen.dart';
import 'pane_preview_screen.dart';
import 'terminal_screen.dart';

const _cacheKeyPrefix = 'tmux-home-cache-v1';

String _agoLabel(double epochSeconds) {
  if (epochSeconds <= 0) return '';
  final seconds = DateTime.now().millisecondsSinceEpoch / 1000 - epochSeconds;
  if (!seconds.isFinite || seconds < 0) return '';
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return '${(seconds / 60).floor()} 分钟前';
  if (seconds < 86400) return '${(seconds / 3600).floor()} 小时前';
  return '${(seconds / 86400).floor()} 天前';
}

String _visitedLabel(double epochSeconds) {
  if (epochSeconds <= 0) return '';
  final at = DateTime.fromMillisecondsSinceEpoch((epochSeconds * 1000).round());
  String two(int value) => value.toString().padLeft(2, '0');
  return '${two(at.month)}-${two(at.day)} ${two(at.hour)}:${two(at.minute)}';
}

Color _statusColor(String status) {
  return switch (status) {
    'busy' => AppTheme.accent,
    'idle' => AppTheme.green,
    _ => AppTheme.faint,
  };
}

String _slice(Object? value, int length) {
  final text = value == null ? '' : '$value';
  return text.length <= length ? text : text.substring(0, length);
}

/// Web's slimBoard, applied before writing the cache.
Map<String, dynamic> slimBoard(Map<String, dynamic> raw) {
  Map<String, dynamic> slimEntry(Map<String, dynamic> entry) {
    final copy = Map<String, dynamic>.from(entry);
    for (final key in ['preview', 'agent_preview']) {
      final preview = entry[key];
      if (preview is Map) {
        final slim = Map<String, dynamic>.from(preview);
        slim['text'] = _slice(preview['text'], 600);
        copy[key] = slim;
      }
    }
    return copy;
  }

  List<Map<String, dynamic>> maps(Object? value) =>
      value is List ? value.whereType<Map>().map((item) => Map<String, dynamic>.from(item)).toList() : const [];
  return {
    ...raw,
    'favorites': maps(raw['favorites']).take(40).map(slimEntry).toList(),
    'recent': maps(raw['recent']).take(4).toList(),
    'cards': maps(raw['cards']).take(40).map((card) {
      final copy = Map<String, dynamic>.from(card);
      copy['body'] = _slice(card['body'], 600);
      return copy;
    }).toList(),
  };
}

class HomeBoardScreen extends StatefulWidget {
  const HomeBoardScreen({super.key, required this.api, required this.baseUrl});

  final Api api;

  /// The server URL: the cache key includes it, so switching servers
  /// never shows another server's board, like web's per-origin storage.
  final String baseUrl;

  @override
  State<HomeBoardScreen> createState() => _HomeBoardScreenState();
}

class _HomeBoardScreenState extends State<HomeBoardScreen> with WidgetsBindingObserver {
  HomeBoard? _board;
  String? _error;
  // Starts false so the initState _load is not blocked by its own guard;
  // _load sets it true synchronously before the first build.
  bool _loading = false;
  bool _stale = false;
  bool _resumed = true;
  Pane? _previewPane;
  String _previewTitle = '';
  Timer? _intervalTimer;
  Timer? _pendingTimer;
  Timer? _retryTimer;

  String get _cacheKey => '$_cacheKeyPrefix:${widget.baseUrl}';

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _resumed = WidgetsBinding.instance.lifecycleState == null ||
        WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;
    _loadCached();
    _load();
    _intervalTimer = Timer.periodic(const Duration(seconds: 15), (_) {
      if (!_resumed) return;
      if (ModalRoute.of(context)?.isCurrent != true) return;
      _load();
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _intervalTimer?.cancel();
    _pendingTimer?.cancel();
    _retryTimer?.cancel();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _resumed = state == AppLifecycleState.resumed;
  }

  Future<void> _loadCached() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(_cacheKey);
      if (raw == null || raw.isEmpty) return;
      final data = jsonDecode(raw);
      if (data is! Map || data['favorites'] is! List) return;
      if (!mounted) return;
      // 实时数据已经先到了就别用缓存覆盖它（慢 prefs 读会让页面倒退成旧数据）。
      if (_board != null) return;
      setState(() {
        _board = HomeBoard.fromJson(Map<String, dynamic>.from(data));
        _stale = true;
      });
    } catch (_) {
      // A corrupt cache is ignored, like web's readCache catch.
    }
  }

  Future<void> _writeCache(Map<String, dynamic> raw) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_cacheKey, jsonEncode(slimBoard(raw)));
    } catch (_) {
      // Cache failure never blocks the board, like web's writeCache catch.
    }
  }

  void _scheduleFollowUps() {
    final board = _board;
    if (board == null) return;
    if (board.favorites.any((favorite) => favorite.pending) && !(_pendingTimer?.isActive ?? false)) {
      _pendingTimer = Timer(const Duration(milliseconds: 2500), () {
        _pendingTimer = null;
        _load();
      });
    }
    if (board.stats.panes == 0 && !(_retryTimer?.isActive ?? false)) {
      _retryTimer = Timer(const Duration(seconds: 4), () {
        _retryTimer = null;
        _load();
      });
    }
  }

  Future<void> _load() async {
    if (_loading || !mounted) return;
    setState(() => _loading = true);
    try {
      final raw = await widget.api.homeBoard();
      await _writeCache(raw);
      if (!mounted) return;
      setState(() {
        _board = HomeBoard.fromJson(raw);
        _error = null;
        _stale = false;
        _loading = false;
      });
      _scheduleFollowUps();
    } catch (error) {
      if (!mounted) return;
      if (_board != null) {
        // Cached board stays on screen; only a toast, like web.
        setState(() => _loading = false);
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
      } else {
        setState(() {
          _error = '$error';
          _loading = false;
        });
      }
    }
  }

  void _openPane(Pane pane) {
    final api = widget.api;
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: api is TerminalApi ? api as TerminalApi : null,
        baseUrl: widget.baseUrl,
      ),
    ));
  }

  /// Web's openFullPreview: wide screens get the right-side drawer, narrow
  /// screens navigate to the preview page.
  void _openFullPreview(Pane pane, String title) {
    if (MediaQuery.of(context).size.width >= 1100) {
      setState(() {
        _previewPane = pane;
        _previewTitle = title;
      });
      return;
    }
    final api = widget.api;
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => PanePreviewScreen(
        api: api,
        pane: pane,
        terminalApi: api is TerminalApi ? api as TerminalApi : null,
        baseUrl: widget.baseUrl,
      ),
    ));
  }

  void _closePreview() => setState(() {
        _previewPane = null;
        _previewTitle = '';
      });

  Future<void> _showPreview(Pane pane, HoverPreview preview, String title) async {
    final origin = preview.offline ? (preview.cached ? '离线 · 缓存' : '离线') : '';
    final time = [origin, _agoLabel(preview.timestamp), if (preview.truncated) '已截断'].where((part) => part.isNotEmpty).join(' · ');
    final role = preview.role == 'assistant' ? 'agent 回复' : preview.role;
    final action = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      builder: (context) => DraggableScrollableSheet(
        initialChildSize: 0.6,
        minChildSize: 0.4,
        maxChildSize: 0.9,
        expand: false,
        builder: (context, scrollController) => Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(title, maxLines: 1, overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600)),
                  ),
                  IconButton(tooltip: '关闭预览', icon: const Icon(Icons.close, size: 20), onPressed: () => Navigator.pop(context)),
                ],
              ),
              const SizedBox(height: 4),
              Text([role, time].where((part) => part.isNotEmpty).join(' · '),
                  style: TextStyle(fontSize: 11.5, color: AppTheme.muted)),
              const SizedBox(height: 12),
              Expanded(
                child: SingleChildScrollView(
                  controller: scrollController,
                  child: SelectableText(preview.text, style: const TextStyle(fontSize: 13.5, height: 1.5)),
                ),
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  OutlinedButton(onPressed: () => Navigator.pop(context, 'full'), child: const Text('完整预览')),
                  const SizedBox(width: 8),
                  Expanded(
                    child: FilledButton(onPressed: () => Navigator.pop(context, 'chat'), child: const Text('聊天')),
                  ),
                  const SizedBox(width: 8),
                  TextButton(onPressed: () => Navigator.pop(context), child: const Text('关闭')),
                ],
              ),
            ],
          ),
        ),
      ),
    );
    if (!mounted) return;
    if (action == 'full') {
      _openFullPreview(pane, title);
    } else if (action == 'chat') {
      _openPane(pane);
    }
  }

  Future<void> _run(Future<void> Function() action, String doneMessage) async {
    try {
      await action();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(doneMessage)));
      await _load();
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
    }
  }

  Future<void> _renameFavorite(HomeFavorite favorite) async {
    final controller = TextEditingController(text: favorite.displayTitle);
    final action = await showDialog<String?>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('修改卡片名'),
        content: TextField(
          key: const Key('rename-title'),
          controller: controller,
          decoration: const InputDecoration(hintText: '留空恢复自动生成的标题'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, null), child: const Text('取消')),
          TextButton(onPressed: () => Navigator.pop(context, ''), child: const Text('恢复默认')),
          FilledButton(onPressed: () => Navigator.pop(context, controller.text.trim()), child: const Text('保存')),
        ],
      ),
    );
    if (action == null || !mounted) return;
    await _run(() => widget.api.renameHomeFavorite(favorite.pane, action), '卡片名已更新');
  }

  Future<void> _addFavorite() async {
    final pane = TextEditingController();
    final note = TextEditingController();
    final title = TextEditingController();
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('添加收藏'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              key: const Key('favorite-pane'),
              controller: pane,
              autocorrect: false,
              decoration: const InputDecoration(hintText: 'pane，例如 %7 或 abc~%7'),
            ),
            const SizedBox(height: 10),
            TextField(controller: title, decoration: const InputDecoration(hintText: '标题（可选）')),
            const SizedBox(height: 10),
            TextField(controller: note, decoration: const InputDecoration(hintText: '备注（可选）')),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('添加')),
        ],
      ),
    );
    if (confirmed != true || pane.text.trim().isEmpty || !mounted) return;
    await _run(
        () => widget.api.addHomeFavorite(pane.text.trim(), note: note.text.trim(), title: title.text.trim()),
        '已添加收藏');
  }

  Widget _section(String title, int count) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 20, 16, 8),
      child: Text('$title $count', style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600)),
    );
  }

  Widget _statusDot(String status) {
    return Container(
      width: 7,
      height: 7,
      decoration: BoxDecoration(shape: BoxShape.circle, color: _statusColor(status)),
    );
  }

  Widget _favoriteCard(HomeFavorite favorite) {
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: InkWell(
        onTap: () => _showPreview(favorite.asPane, favorite.hoverPreview, favorite.displayTitle),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  _statusDot(favorite.status),
                  const SizedBox(width: 7),
                  if (favorite.provider != null && favorite.provider!.isNotEmpty)
                    Chip(label: Text(favorite.provider!), visualDensity: VisualDensity.compact),
                  const Spacer(),
                  if (favorite.cached) Text('缓存', style: TextStyle(fontSize: 11, color: AppTheme.faint)),
                  if (favorite.pending) Text('加载中', style: TextStyle(fontSize: 11, color: AppTheme.faint)),
                  IconButton(
                    key: Key('rename-${favorite.pane}'),
                    tooltip: '修改卡片名',
                    icon: const Icon(Icons.edit_outlined, size: 18),
                    onPressed: () => _renameFavorite(favorite),
                  ),
                  IconButton(
                    key: Key('unfav-${favorite.pane}'),
                    tooltip: '取消收藏',
                    icon: Icon(Icons.star, size: 20, color: AppTheme.accent),
                    onPressed: () => _run(() => widget.api.deleteHomeFavorite(favorite.pane), '已取消收藏'),
                  ),
                ],
              ),
              const SizedBox(height: 6),
              Text(favorite.displayTitle, style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
              const SizedBox(height: 2),
              Text(favorite.meta, style: TextStyle(fontSize: 11.5, color: AppTheme.muted)),
              if (favorite.note.isNotEmpty) ...[
                const SizedBox(height: 6),
                Text(favorite.note, style: TextStyle(fontSize: 12.5, color: AppTheme.muted)),
              ],
              if (favorite.previewText.isNotEmpty) ...[
                const SizedBox(height: 6),
                Text.rich(
                  TextSpan(children: [
                    if (favorite.previewRole.isNotEmpty)
                      TextSpan(text: '${favorite.previewRole} ', style: const TextStyle(fontWeight: FontWeight.w600)),
                    TextSpan(text: favorite.previewText),
                  ]),
                  maxLines: 4,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: 12.5, color: AppTheme.muted),
                ),
              ] else
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text('还没有对话内容', style: TextStyle(fontSize: 12.5, color: AppTheme.faint)),
                ),
              const SizedBox(height: 8),
              Text('${favorite.pane}${favorite.command.isNotEmpty ? ' · ${favorite.command}' : ''}',
                  style: AppTheme.mono.copyWith(fontSize: 10.5, color: AppTheme.faint)),
            ],
          ),
        ),
      ),
    );
  }

  Widget _recentTile(HomeRecent recent) {
    final board = _board;
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: ListTile(
        title: Text(recent.title.isNotEmpty ? recent.title : recent.pane, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(
            [recent.meta, if (_visitedLabel(recent.visitedAt).isNotEmpty) _visitedLabel(recent.visitedAt)]
                .join(' · '),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(fontSize: 11.5, color: AppTheme.muted)),
        leading: _statusDot(recent.status),
        trailing: IconButton(
          key: Key('delete-recent-${recent.pane}'),
          tooltip: '删除记录',
          icon: const Icon(Icons.delete_outline, size: 18),
          onPressed: () => _run(() => widget.api.deleteHomeRecent(recent.pane), '记录已删除'),
        ),
        onTap: board == null
            ? null
            : () => _showPreview(recent.asPane,
                board.previewFor(recent.pane, provider: recent.provider.isEmpty ? null : recent.provider, offline: recent.status == 'offline'),
                recent.title.isNotEmpty ? recent.title : recent.pane),
      ),
    );
  }

  Widget _boardCard(HomeCard card) {
    final board = _board;
    final pane = card.asPane;
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: InkWell(
        onTap: pane == null || board == null
            ? null
            : () => _showPreview(pane, board.previewFor(card.paneId), card.title),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Chip(label: Text(card.kind), visualDensity: VisualDensity.compact),
                  const Spacer(),
                  IconButton(
                    key: Key('delete-card-${card.id}'),
                    tooltip: '删除卡片',
                    icon: const Icon(Icons.delete_outline, size: 18),
                    onPressed: () => _run(() => widget.api.deleteHomeCard(card.id), '卡片已删除'),
                  ),
                ],
              ),
              const SizedBox(height: 6),
              Text(card.title, style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
              if (card.body.isNotEmpty) ...[
                const SizedBox(height: 6),
                Text(card.body, maxLines: 8, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13)),
              ],
              const SizedBox(height: 8),
              Text(card.paneId.isNotEmpty ? card.paneId : '来自 MCP',
                  style: AppTheme.mono.copyWith(fontSize: 10.5, color: AppTheme.faint)),
            ],
          ),
        ),
      ),
    );
  }

  Widget _skeleton() {
    Widget box(double height) => Container(
          height: height,
          margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
          decoration: BoxDecoration(color: AppTheme.line, borderRadius: BorderRadius.circular(12)),
        );
    return ListView(
      padding: const EdgeInsets.only(top: 12, bottom: 16),
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [for (var i = 0; i < 5; i++) Container(width: 80, height: 32, decoration: BoxDecoration(color: AppTheme.line, borderRadius: BorderRadius.circular(16)))],
          ),
        ),
        box(120),
        box(120),
        box(120),
      ],
    );
  }

  Widget _previewPanel(double width) {
    final pane = _previewPane;
    if (pane == null) return const SizedBox.shrink();
    final api = widget.api;
    final panelWidth = width * 0.56 < 760 ? width * 0.56 : 760.0;
    return Align(
      alignment: Alignment.centerRight,
      child: Container(
        key: const Key('preview-panel'),
        width: panelWidth,
        height: double.infinity,
        decoration: BoxDecoration(
          color: AppTheme.page,
          border: Border(left: BorderSide(color: AppTheme.line)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              child: Row(
                children: [
                  Expanded(
                    child: Text(_previewTitle, maxLines: 1, overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600)),
                  ),
                  FavoriteStarButton(api: api, paneRef: pane.ref),
                  if (api is TerminalApi)
                    IconButton(
                      tooltip: '终端',
                      icon: const Icon(Icons.terminal, size: 20),
                      onPressed: () => Navigator.of(context).push(MaterialPageRoute(
                        builder: (_) => TerminalScreen(api: api as TerminalApi, pane: pane),
                      )),
                    ),
                  IconButton(
                    key: const Key('preview-panel-console'),
                    tooltip: '在对话中打开',
                    icon: const Icon(Icons.chat_bubble_outline, size: 20),
                    onPressed: () {
                      _closePreview();
                      _openPane(pane);
                    },
                  ),
                  IconButton(
                    key: const Key('preview-panel-close'),
                    tooltip: '关闭预览',
                    icon: const Icon(Icons.close, size: 20),
                    onPressed: _closePreview,
                  ),
                ],
              ),
            ),
            const Divider(height: 1),
            Expanded(
              child: ChatScreen(
                source: PaneChatSource(api: api, pane: pane),
                terminalApi: api is TerminalApi ? api as TerminalApi : null,
                embedded: true,
                baseUrl: widget.baseUrl,
              ),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final width = MediaQuery.of(context).size.width;
    final wide = width >= 1100;
    final boardBody = _loading && _board == null
        ? _skeleton()
        : _board == null
            ? ListView(
                padding: const EdgeInsets.all(24),
                children: [
                  Text(_error ?? '加载失败', style: TextStyle(color: AppTheme.danger, fontSize: 13)),
                  const SizedBox(height: 12),
                  OutlinedButton(onPressed: _load, child: const Text('重试')),
                ],
              )
            : RefreshIndicator(
                  onRefresh: _load,
                  child: ListView(
                    padding: const EdgeInsets.only(bottom: 16),
                    children: [
                      if (_stale)
                        Padding(
                          padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
                          child: Row(
                            children: [
                              const Icon(Icons.circle, size: 6, color: Color(0xFFC98A2B)),
                              const SizedBox(width: 6),
                              Text('显示缓存，正在更新…', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
                            ],
                          ),
                        ),
                      Padding(
                        padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
                        child: Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            Chip(label: Text('${_board!.stats.panes} panes')),
                            Chip(label: Text('${_board!.stats.busy} busy')),
                            Chip(label: Text('${_board!.stats.idle} idle')),
                            Chip(label: Text('${_board!.stats.favorites} 收藏')),
                            Chip(label: Text('${_board!.stats.cards} 卡片')),
                          ],
                        ),
                      ),
                      _section('收藏', _board!.favorites.length),
                      if (_board!.favorites.isEmpty)
                        Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 16),
                          child: Text('还没有收藏的会话。点右上角的星号添加，或在会话列表里点 pane 的星号。',
                              style: TextStyle(color: AppTheme.muted)),
                        )
                      else
                        for (final favorite in _board!.favorites) _favoriteCard(favorite),
                      _section('最近访问', _board!.recent.length),
                      if (_board!.recent.isEmpty)
                        Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 16),
                          child: Text('还没有访问记录。打开任意 pane 后，最近的 4 个会出现在这里。',
                              style: TextStyle(color: AppTheme.muted)),
                        )
                      else
                        for (final recent in _board!.recent) _recentTile(recent),
                      _section('看板卡片', _board!.cards.length),
                      if (_board!.cards.isEmpty)
                        Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 16),
                          child: Text('看板是空的。在任意 agent pane 里让模型调用 MCP 工具 publish_home_card，就能把结论贴到这里。',
                              style: TextStyle(color: AppTheme.muted)),
                        )
                      else
                        for (final card in _board!.cards) _boardCard(card),
                    ],
                  ),
                );
    final showPanel = wide && _previewPane != null;
    return Scaffold(
      appBar: AppBar(
        title: const Text('主页板'),
        actions: [
          IconButton(
            tooltip: '搜索',
            icon: const Icon(Icons.search, size: 20),
            onPressed: () => Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => HomeSearchScreen(api: widget.api, baseUrl: widget.baseUrl))),
          ),
          IconButton(tooltip: '添加收藏', icon: const Icon(Icons.star_border, size: 20), onPressed: _addFavorite),
          IconButton(tooltip: '刷新', icon: const Icon(Icons.refresh, size: 20), onPressed: _load),
        ],
      ),
      body: showPanel
          ? Stack(
              children: [
                boardBody,
                Positioned.fill(
                  child: GestureDetector(
                    onTap: _closePreview,
                    child: Container(color: const Color.fromRGBO(32, 31, 28, 0.18)),
                  ),
                ),
                _previewPanel(width),
              ],
            )
          : boardBody,
    );
  }
}
