import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../models/models.dart';
import '../theme.dart';
import 'chat_screen.dart';

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
      final hosts = await widget.api.hosts();
      if (!mounted) return;
      setState(() {
        _hosts = hosts;
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

  int get _paneCount => (_hosts ?? const <Host>[]).fold<int>(0, (sum, host) => sum + host.panes.length);

  void _openPane(Pane pane) {
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ChatScreen(source: PaneChatSource(api: widget.api, pane: pane)),
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
            for (final pane in host.panes)
              ListTile(
                key: Key('pane-${pane.ref}'),
                dense: true,
                contentPadding: const EdgeInsets.only(left: 34, right: 16),
                title: Text(pane.label, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13.5)),
                subtitle: Text(
                  [pane.ref, if (pane.session.isNotEmpty) pane.session, if (pane.provider != null) pane.provider!].join(' · '),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontSize: 11, color: AppTheme.faint),
                ),
                trailing: pane.hasTranscript
                    ? const Icon(Icons.chat_bubble_outline, size: 16, color: AppTheme.faint)
                    : const Icon(Icons.terminal, size: 16, color: AppTheme.faint),
                onTap: () => _openPane(pane),
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
