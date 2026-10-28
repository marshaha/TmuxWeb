import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../models/models.dart';
import '../theme.dart';

/// Remote SSH host management: list, add, edit and delete
/// (`/api/remote-hosts`), including the "save without verifying" escape hatch.
class HostsAdminScreen extends StatefulWidget {
  const HostsAdminScreen({super.key, required this.api});

  final Api api;

  @override
  State<HostsAdminScreen> createState() => _HostsAdminScreenState();
}

class _HostsAdminScreenState extends State<HostsAdminScreen> {
  List<RemoteHost>? _hosts;
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
      final hosts = await widget.api.remoteHosts();
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

  Future<void> _openEditor([RemoteHost? host]) async {
    final saved = await showDialog<bool>(
      context: context,
      builder: (_) => _HostEditorDialog(api: widget.api, host: host),
    );
    if (saved == true) await _load();
  }

  Future<void> _delete(RemoteHost host) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('删除 ${host.name}？'),
        content: const Text('只删除 TmuxWeb 里的这台主机记录，远端不会受影响。'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('删除')),
        ],
      ),
    );
    if (confirmed != true) return;
    try {
      await widget.api.deleteRemoteHost(host.id);
      await _load();
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('服务器管理'),
        actions: [IconButton(tooltip: '刷新', onPressed: _load, icon: const Icon(Icons.refresh, size: 20))],
      ),
      floatingActionButton: FloatingActionButton(
        key: const Key('host-add'),
        onPressed: () => _openEditor(),
        child: const Icon(Icons.add),
      ),
      body: RefreshIndicator(onRefresh: _load, child: _buildBody()),
    );
  }

  Widget _buildBody() {
    if (_loading && _hosts == null) return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    final hosts = _hosts;
    if (hosts == null) {
      return ListView(padding: const EdgeInsets.all(24), children: [
        Text(_error ?? '加载失败', style: const TextStyle(color: AppTheme.danger, fontSize: 13)),
        const SizedBox(height: 12),
        OutlinedButton(onPressed: _load, child: const Text('重试')),
      ]);
    }
    if (hosts.isEmpty) {
      return ListView(padding: const EdgeInsets.all(24), children: const [
        Text('还没有远程主机。点右下角 + 添加，远端只需要 SSH 和 tmux。', style: TextStyle(color: AppTheme.muted)),
      ]);
    }
    return ListView.separated(
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 90),
      itemCount: hosts.length,
      separatorBuilder: (_, __) => const SizedBox(height: 10),
      itemBuilder: (context, index) {
        final host = hosts[index];
        return Container(
          key: Key('host-card-${host.id}'),
          padding: const EdgeInsets.fromLTRB(14, 12, 8, 12),
          decoration: BoxDecoration(
            color: AppTheme.card,
            border: Border.all(color: AppTheme.line),
            borderRadius: BorderRadius.circular(14),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(host.name,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
                  ),
                  IconButton(
                    key: Key('host-edit-${host.id}'),
                    tooltip: '编辑',
                    onPressed: () => _openEditor(host),
                    icon: const Icon(Icons.edit_outlined, size: 18),
                  ),
                  IconButton(
                    key: Key('host-delete-${host.id}'),
                    tooltip: '删除',
                    onPressed: () => _delete(host),
                    icon: const Icon(Icons.delete_outline, size: 18),
                  ),
                ],
              ),
              Text('${host.username}@${host.hostname}:${host.port} · ${_authLabel(host.authType)}',
                  style: const TextStyle(fontSize: 12, color: AppTheme.muted)),
              const SizedBox(height: 4),
              Text(host.hasPin ? host.fingerprint : '指纹待首次连通时记录',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: 11,
                    color: host.hasPin ? AppTheme.faint : AppTheme.accent,
                    fontFamily: 'monospace',
                  )),
            ],
          ),
        );
      },
    );
  }

  String _authLabel(String type) => switch (type) {
        'key' => '私钥',
        'agent' => '本机密钥',
        _ => '密码',
      };
}

/// Add or edit one host. A failed fingerprint scan or a wrong password is not
/// fatal when "不校验连接直接保存" is ticked.
class _HostEditorDialog extends StatefulWidget {
  const _HostEditorDialog({required this.api, this.host});

  final Api api;
  final RemoteHost? host;

  @override
  State<_HostEditorDialog> createState() => _HostEditorDialogState();
}

class _HostEditorDialogState extends State<_HostEditorDialog> {
  late final TextEditingController _name = TextEditingController(text: widget.host?.name ?? '');
  late final TextEditingController _hostname = TextEditingController(text: widget.host?.hostname ?? '');
  late final TextEditingController _port = TextEditingController(text: '${widget.host?.port ?? 22}');
  late final TextEditingController _username = TextEditingController(text: widget.host?.username ?? '');
  final TextEditingController _credential = TextEditingController();
  final TextEditingController _key = TextEditingController();
  late String _authType = widget.host?.authType ?? 'password';
  bool _reveal = false;
  bool _skipVerify = false;
  bool _busy = false;
  String _status = '';

  bool get _editing => widget.host != null;

  @override
  void dispose() {
    for (final controller in [_name, _hostname, _port, _username, _credential, _key]) {
      controller.dispose();
    }
    super.dispose();
  }

  Future<void> _save() async {
    final name = _name.text.trim();
    final hostname = _hostname.text.trim();
    final username = _username.text.trim();
    final port = int.tryParse(_port.text.trim()) ?? 22;
    if (name.isEmpty || hostname.isEmpty || username.isEmpty) {
      setState(() => _status = '显示名称、主机/IP、用户名都要填');
      return;
    }
    final secret = _authType == 'key' ? _key.text : _credential.text;
    if (!_editing && _authType != 'agent' && secret.isEmpty) {
      setState(() => _status = '请填写密码或私钥（或选择本机密钥）');
      return;
    }
    setState(() {
      _busy = true;
      _status = '正在保存…';
    });
    try {
      Map<String, dynamic>? scanning;
      final previous = widget.host;
      final moved = previous == null || previous.hostname != hostname || previous.port != port;
      if (moved) {
        try {
          scanning = await widget.api.scanHostKey(hostname, port);
        } catch (error) {
          if (!_skipVerify) rethrow;
          scanning = const {'host_key': '', 'fingerprint': ''};
          if (mounted) setState(() => _status = '读取指纹失败，将在首次连通时自动记录。');
        }
      }
      final payload = <String, dynamic>{
        'name': name,
        'hostname': hostname,
        'port': port,
        'username': username,
        'auth_type': _authType,
        'verify': !_skipVerify,
      };
      if (secret.isNotEmpty) payload['credential'] = secret;
      if (scanning != null) {
        payload['host_key'] = scanning['host_key'] ?? '';
        payload['fingerprint'] = scanning['fingerprint'] ?? '';
        payload['confirm_fingerprint'] = scanning['fingerprint'] ?? '';
      }
      if (_editing) {
        await widget.api.updateRemoteHost(widget.host!.id, payload);
      } else {
        if (_authType == 'agent') payload['credential'] = '';
        await widget.api.createRemoteHost(payload);
      }
      if (!mounted) return;
      Navigator.pop(context, true);
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _status = '$error';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final agent = _authType == 'agent';
    final keyMode = _authType == 'key';
    return AlertDialog(
      title: Text(_editing ? '编辑主机' : '添加主机'),
      content: SizedBox(
        width: 420,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(controller: _name, decoration: const InputDecoration(labelText: '显示名称')),
              const SizedBox(height: 8),
              TextField(
                key: const Key('host-hostname'),
                controller: _hostname,
                autocorrect: false,
                decoration: const InputDecoration(labelText: '主机 / IP'),
              ),
              const SizedBox(height: 8),
              Row(children: [
                SizedBox(
                  width: 96,
                  child: TextField(
                    controller: _port,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(labelText: '端口'),
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: TextField(controller: _username, decoration: const InputDecoration(labelText: '用户名')),
                ),
              ]),
              const SizedBox(height: 8),
              DropdownButtonFormField<String>(
                key: const Key('host-auth-type'),
                initialValue: _authType,
                decoration: const InputDecoration(labelText: '登录方式'),
                items: const [
                  DropdownMenuItem(value: 'password', child: Text('密码')),
                  DropdownMenuItem(value: 'key', child: Text('OpenSSH 私钥')),
                  DropdownMenuItem(value: 'agent', child: Text('本机密钥（SSH Agent）')),
                ],
                onChanged: (value) => setState(() => _authType = value ?? 'password'),
              ),
              if (!agent) ...[
                const SizedBox(height: 8),
                TextField(
                  key: const Key('host-credential'),
                  controller: keyMode ? _key : _credential,
                  minLines: keyMode ? 4 : 1,
                  maxLines: keyMode ? 8 : 1,
                  autocorrect: false,
                  obscureText: !_reveal && !keyMode,
                  decoration: InputDecoration(
                    labelText: keyMode ? '私钥内容' : (_editing ? 'SSH 密码（留空不修改）' : 'SSH 密码'),
                  ),
                ),
                CheckboxListTile(
                  key: const Key('host-reveal'),
                  value: _reveal,
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  controlAffinity: ListTileControlAffinity.leading,
                  title: const Text('显示凭据内容', style: TextStyle(fontSize: 12.5)),
                  onChanged: (value) => setState(() => _reveal = value ?? false),
                ),
              ],
              CheckboxListTile(
                key: const Key('host-skip-verify'),
                value: _skipVerify,
                dense: true,
                contentPadding: EdgeInsets.zero,
                controlAffinity: ListTileControlAffinity.leading,
                title: const Text('不校验连接直接保存', style: TextStyle(fontSize: 12.5)),
                subtitle: const Text('密码/密钥有误或主机暂不可达也能先存下来，指纹在首次连通时记录',
                    style: TextStyle(fontSize: 11, color: AppTheme.faint)),
                onChanged: (value) => setState(() => _skipVerify = value ?? false),
              ),
              if (_status.isNotEmpty)
                Align(
                  alignment: Alignment.centerLeft,
                  child: Padding(
                    padding: const EdgeInsets.only(top: 6),
                    child: Text(_status, style: const TextStyle(fontSize: 12, color: AppTheme.muted)),
                  ),
                ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(onPressed: _busy ? null : () => Navigator.pop(context, false), child: const Text('取消')),
        FilledButton(
          key: const Key('host-save'),
          onPressed: _busy ? null : _save,
          child: _busy
              ? const SizedBox(height: 15, width: 15, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
              : Text(_editing ? '保存' : '添加'),
        ),
      ],
    );
  }
}
