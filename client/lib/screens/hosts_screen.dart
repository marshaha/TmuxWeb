import 'package:flutter/material.dart';

import 'dart:async';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../models/models.dart';
import '../theme.dart';
import 'chat_screen.dart';
import 'hosts_admin_screen.dart';

/// Host → pane browser: the mobile equivalent of the web sidebar.
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
  String? _error;
  bool _loading = true;
  Timer? _stateTimer;

  @override
  void initState() {
    super.initState();
    _load();
    _stateTimer = Timer.periodic(const Duration(seconds: 9), (_) => _refreshStates());
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
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: api is TerminalApi ? api as TerminalApi : null,
      ),
    ));
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
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('会话'),
            Text(widget.baseUrl, style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w400, color: AppTheme.faint)),
          ],
        ),
        actions: [
          IconButton(
            key: const Key('hosts-admin'),
            tooltip: '服务器管理',
            onPressed: () async {
              await Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => HostsAdminScreen(api: widget.api),
              ));
              await _load();
            },
            icon: const Icon(Icons.dns_outlined, size: 20),
          ),
          IconButton(tooltip: '打开分享链接', onPressed: _openShare, icon: const Icon(Icons.link, size: 20)),
          IconButton(tooltip: '刷新', onPressed: _load, icon: const Icon(Icons.refresh, size: 20)),
        ],
      ),
      body: RefreshIndicator(onRefresh: _load, child: _buildBody()),
      bottomNavigationBar: Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
        decoration: const BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
        child: Text(
          _hosts == null ? '' : '${_hosts!.length} 台主机 · $_paneCount 个 pane',
          style: const TextStyle(fontSize: 11.5, color: AppTheme.faint),
        ),
      ),
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
    return ListTile(
      key: Key('pane-${pane.ref}'),
      dense: true,
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
        style: const TextStyle(fontSize: 10.5, color: AppTheme.faint),
      ),
      trailing: pane.hasTranscript
          ? const Icon(Icons.chat_bubble_outline, size: 16, color: AppTheme.faint)
          : const Icon(Icons.terminal, size: 16, color: AppTheme.faint),
      onTap: () => _openPane(pane),
    );
  }

  Widget _buildBody() {
    if (_loading && _hosts == null) return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    if (_hosts == null) {
      return ListView(
        padding: const EdgeInsets.all(24),
        children: [
          Text(_error ?? '加载失败', style: const TextStyle(color: Color(0xFFB34B43), fontSize: 13)),
          const SizedBox(height: 12),
          OutlinedButton(onPressed: _load, child: const Text('重试')),
        ],
      );
    }
    final hosts = _hosts!;
    if (hosts.isEmpty) {
      return ListView(
        padding: const EdgeInsets.all(24),
        children: const [Text('还没有可用的 tmux pane。', style: TextStyle(color: AppTheme.muted))],
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
              Text('${host.panes.length}', style: const TextStyle(fontSize: 11.5, color: AppTheme.faint)),
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
                  const Icon(Icons.folder_outlined, size: 15, color: AppTheme.faint),
                  const SizedBox(width: 7),
                  Flexible(
                    child: Text(session.$1,
                        maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13)),
                  ),
                  const SizedBox(width: 6),
                  Text('${session.$2.length}', style: const TextStyle(fontSize: 10.5, color: AppTheme.faint)),
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
                          style: const TextStyle(fontSize: 12, color: AppTheme.muted)),
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
                          textAlign: TextAlign.center, style: const TextStyle(color: AppTheme.muted, fontSize: 13)),
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
        const Text('访问密码', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
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
