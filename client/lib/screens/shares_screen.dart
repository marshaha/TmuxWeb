import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/api_client.dart';
import '../theme.dart';
import 'hosts_screen.dart';

/// Resolves the URL web's shares page opens for [share].
///
/// Web renders 打开 as `<a href="s.url" target="_blank">` (web/shares.html),
/// and the browser resolves that href against the page origin — the same
/// resolution `new URL(s.url, location.origin).href` gives for the copy
/// action. This mirrors that resolution against [baseUrl]: absolute
/// http(s) URLs pass through, absolute paths join the base, and relative
/// paths resolve under the base. Empty [baseUrl] leaves the URL as-is.
String shareOpenUrl(String baseUrl, Map<String, dynamic> share) {
  final url = '${share['url'] ?? ''}';
  if (url.startsWith('http://') || url.startsWith('https://')) return url;
  var base = baseUrl;
  while (base.endsWith('/')) {
    base = base.substring(0, base.length - 1);
  }
  if (base.isEmpty) return url;
  if (url.startsWith('/')) return '$base$url';
  if (url.isEmpty) return base;
  return '$base/$url';
}

/// Share management, mirroring web/shares.html: summary, cards with
/// status, meta and actions (open, copy, extend, reset password, stop,
/// delete). Creation lives on the chat screen, like web's console
/// share button; this page only manages what exists.
class SharesScreen extends StatefulWidget {
  const SharesScreen({super.key, required this.api, required this.baseUrl, this.tabOpener});

  final Api api;
  final String baseUrl;

  /// Web-target opener for the 打开 action: web's shares page opens a new
  /// browser tab (`<a href="s.url" target="_blank">`), so on the web
  /// target this must be a `package:web window.open(url, '_blank')` call.
  /// It is injected rather than imported here because package:web (via
  /// dart:js_interop) cannot be imported from this shared file without
  /// breaking native/VM compilation; the actual window.open call belongs
  /// in a conditional-import barrel wired in by the caller. Null (the
  /// default, and always the case on native targets) means the in-app
  /// view below is used.
  final void Function(String url)? tabOpener;

  @override
  State<SharesScreen> createState() => _SharesScreenState();
}

class _SharesScreenState extends State<SharesScreen> {
  List<Map<String, dynamic>>? _shares;
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final shares = await widget.api.listShares();
      if (mounted) {
        setState(() {
          _shares = shares;
          _loading = false;
        });
      }
    } catch (error) {
      if (mounted) {
        setState(() {
          _error = '$error';
          _loading = false;
        });
      }
    }
  }

  String _fullUrl(Map<String, dynamic> share) {
    final url = '${share['url'] ?? ''}';
    if (widget.baseUrl.isEmpty) return url;
    if (url.startsWith('/')) return '${widget.baseUrl}$url';
    return url;
  }

  static String _formatTime(dynamic seconds) {
    final value = seconds is num ? seconds.toInt() : int.tryParse('$seconds') ?? 0;
    final time = DateTime.fromMillisecondsSinceEpoch(value * 1000);
    String two(int number) => number.toString().padLeft(2, '0');
    // Web uses toLocaleString(): local date + time WITH seconds.
    // Dart has no locale toLocaleString, so the same fields in the
    // same order, with seconds added (the previous format dropped
    // them).
    return '${time.year}-${two(time.month)}-${two(time.day)} ${two(time.hour)}:${two(time.minute)}:${two(time.second)}';
  }

  static (String, Color) _status(Map<String, dynamic> share) {
    switch (share['status']) {
      case 'active':
        return ('有效', AppTheme.green);
      case 'expired':
        return ('已过期', AppTheme.accent);
      default:
        return ('已停止', AppTheme.danger);
    }
  }

  void _snack(String message) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  Future<bool> _confirm(String message, String action) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        content: Text(message),
        actions: [
          TextButton(onPressed: () => Navigator.of(context).pop(false), child: const Text('取消')),
          TextButton(onPressed: () => Navigator.of(context).pop(true), child: Text(action)),
        ],
      ),
    );
    return confirmed ?? false;
  }

  Future<void> _extend(Map<String, dynamic> share) async {
    var seconds = 86400;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: const Text('续期分享'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              DropdownButton<int>(
                key: const Key('share-extend-seconds'),
                value: seconds,
                isExpanded: true,
                items: const [
                  DropdownMenuItem(value: 3600, child: Text('增加 1 小时')),
                  DropdownMenuItem(value: 86400, child: Text('增加 1 天')),
                  DropdownMenuItem(value: 604800, child: Text('增加 7 天')),
                  DropdownMenuItem(value: 2592000, child: Text('增加 30 天')),
                ],
                onChanged: (value) {
                  if (value != null) setDialogState(() => seconds = value);
                },
              ),
              const SizedBox(height: 8),
              Text('从当前到期时间继续增加；已过期分享从现在开始计算。',
                  style: TextStyle(fontSize: 12, color: AppTheme.muted)),
            ],
          ),
        actions: [
          TextButton(onPressed: () => Navigator.of(context).pop(false), child: const Text('取消')),
          FilledButton(
            key: const Key('share-extend-confirm'),
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('确认续期'),
          ),
        ],
        ),
      ),
    );
    if (confirmed != true || !mounted) return;
    try {
      await widget.api.extendShare('${share['id']}', seconds);
      _snack('续期成功');
      await _load();
    } catch (error) {
      _snack('$error');
    }
  }

  Future<void> _resetPassword(Map<String, dynamic> share) async {
    final controller = TextEditingController();
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('重置访问密码'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              key: const Key('share-new-password'),
              controller: controller,
              obscureText: true,
              decoration: const InputDecoration(hintText: '新密码；留空则取消密码'),
            ),
            const SizedBox(height: 8),
            Text('重置后，所有旧访客登录立即失效。', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(context).pop(false), child: const Text('取消')),
          FilledButton(
            key: const Key('share-password-save'),
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('保存密码'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    try {
      await widget.api.resetSharePassword('${share['id']}', controller.text);
      _snack('密码已更新');
      await _load();
    } catch (error) {
      _snack('$error');
    }
  }

  Future<void> _stop(Map<String, dynamic> share) async {
    if (!await _confirm('立即停止后，访客将无法继续访问。确定吗？', '提前停止')) return;
    try {
      await widget.api.stopShare('${share['id']}');
      _snack('分享已停止');
      await _load();
    } catch (error) {
      _snack('$error');
    }
  }

  Future<void> _delete(Map<String, dynamic> share) async {
    if (!await _confirm('永久删除这条分享记录？此操作不可撤销。', '删除记录')) return;
    try {
      await widget.api.deleteShare('${share['id']}');
      _snack('记录已删除');
      await _load();
    } catch (error) {
      _snack('$error');
    }
  }

  void _openShare(Map<String, dynamic> share) {
    // Web parity for 打开: web/shares.html opens a new browser tab with
    // the share URL resolved against the origin (see [shareOpenUrl]).
    // On the web target that tab open is [SharesScreen.tabOpener]; while
    // no opener is wired (and always on native targets, where there are
    // no browser tabs) the in-app ShareScreen below is the platform
    // equivalent — the same share viewed inside the app, not a new tab.
    if (kIsWeb && widget.tabOpener != null) {
      widget.tabOpener!(shareOpenUrl(widget.baseUrl, share));
      return;
    }
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => ShareScreen(api: widget.api, slug: '${share['slug']}'),
      ),
    );
  }

  Widget _actionButton(String label, VoidCallback? onPressed, {Key? key, Color? color}) => OutlinedButton(
        key: key,
        onPressed: onPressed,
        style: color == null ? null : OutlinedButton.styleFrom(foregroundColor: color),
        child: Text(label),
      );

  Widget _shareCard(Map<String, dynamic> share) {
    final id = '${share['id']}';
    final status = _status(share);
    final stopped = share['status'] == 'stopped';
    return Card(
      margin: const EdgeInsets.fromLTRB(16, 0, 16, 12),
      color: AppTheme.card,
      shape: RoundedRectangleBorder(
        side: BorderSide(color: AppTheme.line),
        borderRadius: BorderRadius.circular(16),
      ),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      // Web renders ONLY s.title: an empty title
                      // stays empty, it never falls back to the slug.
                      Text('${share['title'] ?? ''}',
                          style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 15)),
                      const SizedBox(height: 3),
                      Text(_fullUrl(share),
                          style: TextStyle(fontSize: 12, color: AppTheme.muted, fontFamily: 'monospace')),
                    ],
                  ),
                ),
                const SizedBox(width: 10),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                  decoration: BoxDecoration(
                    color: status.$2.withValues(alpha: 0.15),
                    borderRadius: BorderRadius.circular(999),
                  ),
                  child: Text(status.$1, style: TextStyle(fontSize: 11, color: status.$2)),
                ),
              ],
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 16,
              runSpacing: 4,
              children: [
                Text('路径：${share['slug'] ?? ''}', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
                Text('Pane：${share['pane_id'] ?? ''}', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
                Text('到期：${_formatTime(share['expires_at'])}',
                    style: TextStyle(fontSize: 12, color: AppTheme.muted)),
                Text(share['password_protected'] == true ? '密码保护' : '免密码',
                    style: TextStyle(fontSize: 12, color: AppTheme.muted)),
                Text(share['allow_input'] == true ? '可对话' : '只读分享',
                    style: TextStyle(fontSize: 12, color: AppTheme.muted)),
              ],
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 7,
              runSpacing: 7,
              children: [
                _actionButton(
                  '打开',
                  key: Key('share-open-$id'),
                  () => _openShare(share),
                ),
                _actionButton(
                  '复制链接',
                  key: Key('share-copy-$id'),
                  () async {
                    await Clipboard.setData(ClipboardData(text: _fullUrl(share)));
                    _snack('链接已复制');
                  },
                ),
                if (!stopped) ...[
                  _actionButton('续期', key: Key('share-extend-$id'), () => _extend(share)),
                  _actionButton('重置密码', key: Key('share-password-$id'), () => _resetPassword(share)),
                  _actionButton('提前停止', key: Key('share-stop-$id'), () => _stop(share), color: AppTheme.accent),
                ],
                _actionButton('删除记录', key: Key('share-delete-$id'), () => _delete(share), color: AppTheme.danger),
              ],
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final shares = _shares;
    return Scaffold(
      appBar: AppBar(
        title: const Text('分享管理'),
        actions: [IconButton(tooltip: '刷新', onPressed: _load, icon: const Icon(Icons.refresh, size: 20))],
      ),
      body: _loading && shares == null
          ? const Center(child: CircularProgressIndicator(strokeWidth: 2))
          : _error != null && shares == null
              ? Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(_error!, style: TextStyle(color: AppTheme.danger, fontSize: 13)),
                      const SizedBox(height: 12),
                      OutlinedButton(onPressed: _load, child: const Text('重试')),
                    ],
                  ),
                )
              : ListView(
                  padding: EdgeInsets.fromLTRB(
                    0,
                    16,
                    0,
                    // Bottom safe-area padding on top of the list's
                    // own bottom inset, so the last card clears the
                    // home indicator / nav bar.
                    16 + MediaQuery.of(context).padding.bottom,
                  ),
                  children: [
                    Padding(
                      padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
                      child: Text(
                        '共 ${shares!.length} 个分享 · ${shares.where((share) => share['status'] == 'active').length} 个有效',
                        style: TextStyle(fontSize: 12, color: AppTheme.muted),
                      ),
                    ),
                    if (shares.isEmpty)
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 60, horizontal: 20),
                        child: Center(child: Text('还没有分享记录。', style: TextStyle(color: AppTheme.muted))),
                      )
                    else
                      for (final share in shares) _shareCard(share),
                  ],
                ),
    );
  }
}
