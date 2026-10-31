/// Home board: the Flutter equivalent of web/home.html.
///
/// Sections mirror /api/home: stats, favorites, recent (4), cards.
/// Differences from web, kept deliberate and documented:
/// - No cached first paint and no 15s board timer: pull-to-refresh and the
///   refresh button reload; the console tree (HostsScreen) still polls states.
/// - A favorite opens its chat directly; web shows a preview drawer first
///   and only then the console. The preview text is on the card itself.
/// - Cards are created by the MCP publish_home_card tool, not by a UI form,
///   exactly like web: there is no "new card" button here.
/// - Adding a favorite from this screen asks for the pane reference
///   (hosts list shows it under each pane); starring from the hosts tree
///   is a follow-up, not in this batch.
library;

import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../models/home_board.dart';
import '../models/models.dart';
import '../theme.dart';
import 'chat_screen.dart';
import 'home_search_screen.dart';

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

class HomeBoardScreen extends StatefulWidget {
  const HomeBoardScreen({super.key, required this.api});

  final Api api;

  @override
  State<HomeBoardScreen> createState() => _HomeBoardScreenState();
}

class _HomeBoardScreenState extends State<HomeBoardScreen> {
  HomeBoard? _board;
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => _loading = true);
    try {
      final board = HomeBoard.fromJson(await widget.api.homeBoard());
      if (!mounted) return;
      setState(() {
        _board = board;
        _error = null;
        _loading = false;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _error = '$error';
        _loading = false;
      });
    }
  }

  void _openPane(Pane pane) {
    final api = widget.api;
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: api is TerminalApi ? api as TerminalApi : null,
      ),
    ));
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
        onTap: () => _openPane(favorite.asPane),
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
                  if (favorite.cached) const Text('缓存', style: TextStyle(fontSize: 11, color: AppTheme.faint)),
                  if (favorite.pending) const Text('加载中', style: TextStyle(fontSize: 11, color: AppTheme.faint)),
                  IconButton(
                    key: Key('rename-${favorite.pane}'),
                    tooltip: '修改卡片名',
                    icon: const Icon(Icons.edit_outlined, size: 18),
                    onPressed: () => _renameFavorite(favorite),
                  ),
                  IconButton(
                    key: Key('unfav-${favorite.pane}'),
                    tooltip: '取消收藏',
                    icon: const Icon(Icons.star, size: 20, color: AppTheme.accent),
                    onPressed: () => _run(() => widget.api.deleteHomeFavorite(favorite.pane), '已取消收藏'),
                  ),
                ],
              ),
              const SizedBox(height: 6),
              Text(favorite.displayTitle, style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
              const SizedBox(height: 2),
              Text(favorite.meta, style: const TextStyle(fontSize: 11.5, color: AppTheme.muted)),
              if (favorite.note.isNotEmpty) ...[
                const SizedBox(height: 6),
                Text(favorite.note, style: const TextStyle(fontSize: 12.5, color: AppTheme.muted)),
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
                  style: const TextStyle(fontSize: 12.5, color: AppTheme.muted),
                ),
              ] else
                const Padding(
                  padding: EdgeInsets.only(top: 6),
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
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: ListTile(
        title: Text(recent.title.isNotEmpty ? recent.title : recent.pane, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(
            [recent.meta, if (_visitedLabel(recent.visitedAt).isNotEmpty) _visitedLabel(recent.visitedAt)]
                .join(' · '),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontSize: 11.5, color: AppTheme.muted)),
        leading: _statusDot(recent.status),
        trailing: IconButton(
          key: Key('delete-recent-${recent.pane}'),
          tooltip: '删除记录',
          icon: const Icon(Icons.delete_outline, size: 18),
          onPressed: () => _run(() => widget.api.deleteHomeRecent(recent.pane), '记录已删除'),
        ),
        onTap: () => _openPane(recent.asPane),
      ),
    );
  }

  Widget _boardCard(HomeCard card) {
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: InkWell(
        onTap: card.asPane == null ? null : () => _openPane(card.asPane!),
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

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('主页板'),
        actions: [
          IconButton(
            tooltip: '搜索',
            icon: const Icon(Icons.search, size: 20),
            onPressed: () => Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => HomeSearchScreen(api: widget.api))),
          ),
          IconButton(tooltip: '添加收藏', icon: const Icon(Icons.star_border, size: 20), onPressed: _addFavorite),
          IconButton(tooltip: '刷新', icon: const Icon(Icons.refresh, size: 20), onPressed: _load),
        ],
      ),
      body: _loading && _board == null
          ? const Center(child: CircularProgressIndicator(strokeWidth: 2))
          : _board == null
              ? ListView(
                  padding: const EdgeInsets.all(24),
                  children: [
                    Text(_error ?? '加载失败', style: const TextStyle(color: Color(0xFFB34B43), fontSize: 13)),
                    const SizedBox(height: 12),
                    OutlinedButton(onPressed: _load, child: const Text('重试')),
                  ],
                )
              : RefreshIndicator(
                  onRefresh: _load,
                  child: ListView(
                    padding: const EdgeInsets.only(bottom: 16),
                    children: [
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
                        const Padding(
                          padding: EdgeInsets.symmetric(horizontal: 16),
                          child: Text('还没有收藏的会话。点右上角的星号添加，或在 web 控制台里点 pane 的星号。',
                              style: TextStyle(color: AppTheme.muted)),
                        )
                      else
                        for (final favorite in _board!.favorites) _favoriteCard(favorite),
                      _section('最近访问', _board!.recent.length),
                      if (_board!.recent.isEmpty)
                        const Padding(
                          padding: EdgeInsets.symmetric(horizontal: 16),
                          child: Text('还没有访问记录。打开任意 pane 后，最近的 4 个会出现在这里。',
                              style: TextStyle(color: AppTheme.muted)),
                        )
                      else
                        for (final recent in _board!.recent) _recentTile(recent),
                      _section('看板卡片', _board!.cards.length),
                      if (_board!.cards.isEmpty)
                        const Padding(
                          padding: EdgeInsets.symmetric(horizontal: 16),
                          child: Text('看板是空的。在任意 agent pane 里让模型调用 MCP 工具 publish_home_card，就能把结论贴到这里。',
                              style: TextStyle(color: AppTheme.muted)),
                        )
                      else
                        for (final card in _board!.cards) _boardCard(card),
                    ],
                  ),
                ),
    );
  }
}
