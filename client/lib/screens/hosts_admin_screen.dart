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
      builder: (_) => _HostEditorDialog(api: widget.api, host: host, allHosts: _hosts ?? const []),
    );
    if (saved == true) await _load();
  }

  /// Web's moveHost: swap the id in the order list, POST the whole order,
  /// reload on success; on failure the list stays as it was, like web,
  /// which only re-renders on a successful load().
  Future<void> _moveHost(int index, int direction) async {
    final hosts = _hosts;
    if (hosts == null) return;
    final target = index + direction;
    if (index < 0 || target < 0 || target >= hosts.length) return;
    final order = hosts.map((host) => host.id).toList();
    order.insert(target, order.removeAt(index));
    try {
      await widget.api.reorderRemoteHosts(order);
      await _load();
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('排序失败：$error')));
    }
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
                    key: Key('host-move-up-${host.id}'),
                    tooltip: '上移',
                    onPressed: index == 0 ? null : () => _moveHost(index, -1),
                    icon: const Icon(Icons.arrow_upward, size: 18),
                  ),
                  IconButton(
                    key: Key('host-move-down-${host.id}'),
                    tooltip: '下移',
                    onPressed: index == hosts.length - 1 ? null : () => _moveHost(index, 1),
                    icon: const Icon(Icons.arrow_downward, size: 18),
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
              if (host.jumpPath.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text('经由 ${host.jumpPath}', style: const TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                ),
              if (host.tmuxPath.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text('tmux: ${host.tmuxPath}', style: const TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                ),
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
///
/// Jump, following web/hosts.html: either pick a saved host as the hop, or
/// type "user@host:port" directly; a typed spec wins and clears the pick,
/// and picking a host clears the spec. The same rule applies to the save
/// and test payloads.
class _HostEditorDialog extends StatefulWidget {
  const _HostEditorDialog({required this.api, this.host, this.allHosts = const []});

  final Api api;
  final RemoteHost? host;
  final List<RemoteHost> allHosts;

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
  late final TextEditingController _jumpSpec = TextEditingController(text: widget.host?.jumpSpec ?? '');
  late String _authType = widget.host?.authType ?? 'password';
  late String _jumpHostId =
      (widget.host?.jumpSpec.isNotEmpty ?? false) ? '' : (widget.host?.jumpHostId ?? '');
  bool _reveal = false;
  bool _skipVerify = false;
  bool _busy = false;
  bool _testing = false;
  String _status = '';

  bool get _editing => widget.host != null;

  @override
  void dispose() {
    for (final controller in [_name, _hostname, _port, _username, _credential, _key, _jumpSpec]) {
      controller.dispose();
    }
    super.dispose();
  }

  Map<String, dynamic> _formPayload(String secret) {
    final spec = _jumpSpec.text.trim();
    return {
      'name': _name.text.trim(),
      'hostname': _hostname.text.trim(),
      'port': int.tryParse(_port.text.trim()) ?? 22,
      'username': _username.text.trim(),
      'auth_type': _authType,
      'credential': secret,
      'jump_host_id': spec.isNotEmpty ? '' : _jumpHostId,
      'jump_spec': spec,
    };
  }

  /// Web's test button on the add form: scan the host key, then POST
  /// /api/remote-hosts/test with the form payload + scanned keys, and
  /// report pane count and sessions in web's wording. Web caches a scan
  /// per host:port; here the test always re-scans, same result, one
  /// more request. Note the server's test handler builds its temporary
  /// host without the jump fields, so a test through a jump is still
  /// the direct check web performs — the jump itself is verified by
  /// saving and connecting, not by this button.
  Future<void> _test() async {
    final name = _name.text.trim();
    final hostname = _hostname.text.trim();
    final username = _username.text.trim();
    if (name.isEmpty || hostname.isEmpty || username.isEmpty) {
      setState(() => _status = '显示名称、主机/IP、用户名都要填');
      return;
    }
    final secret = _authType == 'key' ? _key.text : _credential.text;
    if (_authType != 'agent' && secret.isEmpty) {
      setState(() => _status = _editing
          ? '编辑时留空表示不修改已保存凭据，无法用空凭据测试；填入凭据后再测试'
          : '请填写密码或私钥（或选择本机密钥）');
      return;
    }
    setState(() {
      _testing = true;
      _status = '正在测试 SSH 登录和远程 tmux…';
    });
    try {
      final scanning = await widget.api.scanHostKey(hostname, int.tryParse(_port.text.trim()) ?? 22);
      final payload = _formPayload(secret)
        ..['host_key'] = scanning['host_key'] ?? ''
        ..['fingerprint'] = scanning['fingerprint'] ?? ''
        ..['confirm_fingerprint'] = scanning['fingerprint'] ?? '';
      final result = await widget.api.testRemoteHost(payload);
      if (!mounted) return;
      final sessions = (result['sessions'] as List?)?.map((session) => '$session').toList() ?? const [];
      setState(() {
        _testing = false;
        _status = '联通成功：SSH 正常，发现 ${result['pane_count'] ?? 0} 个 pane，session：${sessions.join(', ').isEmpty ? '无' : sessions.join(', ')}';
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _testing = false;
        _status = '联通失败：$error';
      });
    }
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
      final spec = _jumpSpec.text.trim();
      final payload = <String, dynamic>{
        'name': name,
        'hostname': hostname,
        'port': port,
        'username': username,
        'auth_type': _authType,
        'verify': !_skipVerify,
        'jump_host_id': spec.isNotEmpty ? '' : _jumpHostId,
        'jump_spec': spec,
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

  /// Label above the box: the dense filled theme made floating labels overlap.
  Widget _field(String label, Widget child) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(left: 2, bottom: 4),
            child: Text(label, style: const TextStyle(fontSize: 11.5, color: AppTheme.muted)),
          ),
          child,
          const SizedBox(height: 10),
        ],
      );

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
              _field('显示名称', TextField(controller: _name, decoration: const InputDecoration(hintText: 'build-box'))),
              _field(
                '主机 / IP',
                TextField(
                  key: const Key('host-hostname'),
                  controller: _hostname,
                  autocorrect: false,
                  decoration: const InputDecoration(hintText: '10.0.0.8'),
                ),
              ),
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  SizedBox(
                    width: 104,
                    child: _field(
                      '端口',
                      TextField(
                        controller: _port,
                        keyboardType: TextInputType.number,
                        decoration: const InputDecoration(hintText: '22'),
                      ),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: _field('用户名', TextField(controller: _username, decoration: const InputDecoration(hintText: 'ubuntu'))),
                  ),
                ],
              ),
              _field(
                '登录方式',
                DropdownButtonFormField<String>(
                  key: const Key('host-auth-type'),
                  initialValue: _authType,
                  decoration: const InputDecoration(),
                  items: const [
                    DropdownMenuItem(value: 'password', child: Text('密码')),
                    DropdownMenuItem(value: 'key', child: Text('OpenSSH 私钥')),
                    DropdownMenuItem(value: 'agent', child: Text('本机密钥（SSH Agent）')),
                  ],
                  onChanged: (value) => setState(() => _authType = value ?? 'password'),
                ),
              ),
              _field(
                '跳板机',
                DropdownButtonFormField<String>(
                  key: const Key('host-jump-host'),
                  initialValue: _jumpHostId,
                  decoration: const InputDecoration(),
                  items: [
                    const DropdownMenuItem(value: '', child: Text('直连（不经过跳板机）')),
                    for (final other in widget.allHosts.where((host) => host.id != widget.host?.id))
                      DropdownMenuItem(value: other.id, child: Text('${other.name} · ${other.hostname}')),
                  ],
                  onChanged: (value) {
                    // Picking a host clears a typed spec, like web.
                    if (value != null && value.isNotEmpty) _jumpSpec.clear();
                    setState(() => _jumpHostId = value ?? '');
                  },
                ),
              ),
              _field(
                '或直接填 user@host:port',
                TextField(
                  key: const Key('host-jump-spec'),
                  controller: _jumpSpec,
                  autocorrect: false,
                  decoration: const InputDecoration(hintText: '不保存为独立主机时用，复用本机凭据'),
                  onChanged: (value) {
                    // A typed spec wins over the pick, like web.
                    if (value.trim().isNotEmpty && _jumpHostId.isNotEmpty) {
                      setState(() => _jumpHostId = '');
                    }
                  },
                ),
              ),
              if (!agent) ...[
                _field(
                  keyMode ? '私钥内容' : (_editing ? 'SSH 密码（留空不修改）' : 'SSH 密码'),
                  TextField(
                    key: const Key('host-credential'),
                    controller: keyMode ? _key : _credential,
                    minLines: keyMode ? 4 : 1,
                    maxLines: keyMode ? 8 : 1,
                    autocorrect: false,
                    obscureText: !_reveal && !keyMode,
                    decoration: const InputDecoration(),
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
        TextButton(onPressed: (_busy || _testing) ? null : () => Navigator.pop(context, false), child: const Text('取消')),
        TextButton(
          key: const Key('host-test'),
          onPressed: (_busy || _testing) ? null : _test,
          child: _testing
              ? const SizedBox(height: 15, width: 15, child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('测试联通'),
        ),
        FilledButton(
          key: const Key('host-save'),
          onPressed: (_busy || _testing) ? null : _save,
          child: _busy
              ? const SizedBox(height: 15, width: 15, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
              : Text(_editing ? '保存' : '添加'),
        ),
      ],
    );
  }
}
