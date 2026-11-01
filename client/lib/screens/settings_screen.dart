import 'dart:convert';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/api_client.dart';
import '../services/config_crypto.dart';
import '../services/save_file.dart';
import '../services/config_import.dart';
import '../theme.dart';
import 'hosts_admin_screen.dart';
import 'shares_screen.dart';
import 'login_screen.dart';

/// Settings, mirroring web/assets/settings.js: server and share
/// management links, password change, encrypted export/import, plugin
/// manager, themes, and logout. Wording follows the web panel so the
/// two surfaces report the same thing.
class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key, required this.api, required this.baseUrl});

  final Api api;
  final String baseUrl;

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  Map<String, dynamic>? _info;
  List<Map<String, dynamic>>? _plugins;
  String? _pluginsError;
  final Map<String, String> _pluginErrors = {};

  final _pwCurrent = TextEditingController();
  final _pwNew = TextEditingController();
  final _pwConfirm = TextEditingController();
  String _pwStatus = '';
  bool _pwBusy = false;

  final _exportPassword = TextEditingController();
  String _exportStatus = '';
  bool _exportBusy = false;

  String? _importFileName;
  List<int>? _importBytes;
  final _importPassword = TextEditingController();
  String _importStatus = '';
  List<String> _importFailures = [];
  bool _importBusy = false;

  @override
  void initState() {
    super.initState();
    _loadInfo();
    _loadPlugins();
  }

  @override
  void dispose() {
    _pwCurrent.dispose();
    _pwNew.dispose();
    _pwConfirm.dispose();
    _exportPassword.dispose();
    _importPassword.dispose();
    super.dispose();
  }

  Future<void> _loadInfo() async {
    try {
      final info = await widget.api.settingsInfo();
      if (mounted) setState(() => _info = info);
    } catch (_) {
      // the password section shows its own fallback
    }
  }

  Future<void> _loadPlugins() async {
    try {
      final plugins = await widget.api.plugins();
      if (mounted) setState(() => _plugins = plugins);
    } catch (error) {
      if (mounted) setState(() => _pluginsError = '$error');
    }
  }

  String get _passwordSource {
    final source = _info?['password_source'];
    if (source == 'file') return '已自定义（哈希保存在 passwords.json，可用这里再次修改）';
    if (source == 'env') return '环境变量 TMUX_WEB_PASSWORD（改一次后即保存到 passwords.json）';
    return '读取中…';
  }

  Future<void> _changePassword() async {
    final current = _pwCurrent.text;
    final next = _pwNew.text;
    final again = _pwConfirm.text;
    if (next != again) {
      setState(() => _pwStatus = '两次输入的新密码不一致');
      return;
    }
    setState(() {
      _pwBusy = true;
      _pwStatus = '正在修改…';
    });
    try {
      await widget.api.changePassword(current, next);
      if (!mounted) return;
      setState(() {
        _pwBusy = false;
        _pwStatus = '密码已更新，其他设备需要重新登录；这个设备保持登录';
      });
      _pwCurrent.clear();
      _pwNew.clear();
      _pwConfirm.clear();
      await _loadInfo();
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _pwBusy = false;
        _pwStatus = '$error';
      });
    }
  }

  Future<void> _selectTheme(AppPalette palette) async {
    // Optimistic paint, like web's TmuxTheme.set: the local switch is
    // instant even on a slow link, and the account sync runs behind it.
    AppTheme.set(palette);
    setState(() {});
    await AppTheme.cache(widget.baseUrl, palette.name);
    try {
      await widget.api.updateTheme(palette.name);
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('主题已切换，同步到账号失败，下次打开会重试')));
    }
  }

  Future<void> _togglePlugin(Map<String, dynamic> plugin, bool wanted) async {
    final name = '${plugin['name'] ?? ''}';
    try {
      await widget.api.setPluginEnabled(name, wanted);
      _pluginErrors.remove(name);
      await _loadPlugins();
    } catch (error) {
      if (!mounted) return;
      setState(() => _pluginErrors[name] = '$error');
      await _loadPlugins();
    }
  }

  Future<void> _export(String which) async {
    final password = _exportPassword.text;
    if (password.length < 6) {
      setState(() => _exportStatus = '导出密码至少 6 位');
      return;
    }
    setState(() {
      _exportBusy = true;
      _exportStatus = '正在收集并加密…';
    });
    try {
      final bundle = <String, dynamic>{
        'app': 'TmuxWeb',
        'exported_at': DateTime.now().toIso8601String(),
        'scope': which,
      };
      var hostCount = 0;
      if (which != 'grid') {
        final data = await widget.api.settingsExport();
        final hosts = data['hosts'];
        bundle['hosts'] = hosts ?? [];
        hostCount = hosts is List ? hosts.length : 0;
      }
      var panes = 0;
      if (which != 'hosts') {
        // The grid page is not ported yet (batch 6), so there is no
        // grid state to export; web exports null here when nothing
        // was stored, and this does the same.
        final prefs = await SharedPreferences.getInstance();
        bundle['grid'] = _storedJson(prefs, 'tmux-grid-v1');
        bundle['grid_presets'] = _storedJson(prefs, 'tmux-grid-presets-v1');
        final grid = bundle['grid'];
        if (grid is Map && grid['panes'] is List) panes = (grid['panes'] as List).length;
      }
      final envelope = ConfigCrypto.encrypt(bundle, password);
      final stamp = DateTime.now().toIso8601String().substring(0, 19).replaceAll(RegExp(r'[:T]'), '-');
      // saveFileBytes writes synchronously on both platforms (sync
      // dart:io natively, sync DOM calls on web), so this still
      // completes under flutter_tester's fake async, where async
      // dart:io never does.
      final saved = await saveFileBytes(
        'tmuxweb-$which-$stamp.tmuxweb.json',
        utf8.encode(const JsonEncoder.withIndent('  ').convert(envelope)),
      );
      if (!mounted) return;
      final label = which == 'grid' ? '分窗布局' : which == 'hosts' ? '服务器配置' : '全部';
      setState(() {
        _exportBusy = false;
        _exportStatus = '已加密导出 $label${hostCount > 0 ? '（$hostCount 台主机）' : ''}'
            '${panes > 0 ? '（$panes 个窗口）' : ''}，导入需要同一个密码\n已保存到 $saved';
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _exportBusy = false;
        _exportStatus = '导出失败：$error';
      });
    }
  }

  dynamic _storedJson(SharedPreferences prefs, String key) {
    final raw = prefs.getString(key);
    if (raw == null) return null;
    try {
      return jsonDecode(raw);
    } catch (_) {
      return null;
    }
  }

  Future<void> _pickImportFile() async {
    final picked = await FilePicker.pickFiles();
    if (picked.isEmpty) return;
    final bytes = await picked.first.xFile.readAsBytes();
    setState(() {
      _importFileName = picked.first.name;
      _importBytes = bytes;
    });
  }

  Future<void> _import() async {
    final bytes = _importBytes;
    if (bytes == null) {
      setState(() => _importStatus = '先选择要导入的文件');
      return;
    }
    final password = _importPassword.text;
    if (password.isEmpty) {
      setState(() => _importStatus = '输入导出时设置的密码');
      return;
    }
    setState(() {
      _importBusy = true;
      _importStatus = '正在解密…';
      _importFailures = [];
    });
    try {
      late Map<String, dynamic> envelope;
      try {
        envelope = jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
      } catch (_) {
        throw ConfigCryptoException('文件不是合法 JSON');
      }
      final bundle = ConfigCrypto.decrypt(envelope, password);
      final prefs = await SharedPreferences.getInstance();
      final result = await importConfigBundle(widget.api, bundle, prefs);
      if (!mounted) return;
      setState(() {
        _importBusy = false;
        _importStatus = result.lines.isNotEmpty ? result.lines.join('；') : '文件里没有可导入的内容';
        _importFailures = result.failures;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _importBusy = false;
        _importStatus = '导入失败：$error';
      });
    }
  }

  Future<void> _logout() async {
    await widget.api.logout();
    if (!mounted) return;
    await Navigator.of(context).pushReplacement(
      MaterialPageRoute(builder: (_) => const LoginScreen()),
    );
  }

  Widget _sectionTitle(String title, [String? subtitle]) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 20, 16, 4),
        child: Row(
          children: [
            Expanded(child: Text(title, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600))),
            if (subtitle != null)
              Text(subtitle, style: TextStyle(fontSize: 12, color: AppTheme.muted)),
          ],
        ),
      );

  Widget _status(String text, {bool good = false}) {
    if (text.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Text(text,
          style: TextStyle(fontSize: 12, color: good ? AppTheme.green : AppTheme.muted)),
    );
  }

  Widget _field(String label, Widget child) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(left: 2, bottom: 4),
            child: Text(label, style: TextStyle(fontSize: 11.5, color: AppTheme.muted)),
          ),
          child,
          const SizedBox(height: 10),
        ],
      );

  @override
  Widget build(BuildContext context) {
    final plugins = _plugins;
    final enabledCount = plugins?.where((plugin) => plugin['enabled'] == true).length ?? 0;
    final current = AppTheme.current;
    return Scaffold(
      appBar: AppBar(title: const Text('设置')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(0, 8, 0, 24),
        children: [
          if (_info != null && _info!['hosts'] != null)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
              child: Text('已连接 ${_info!['hosts']} 台远程主机', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
            ),
          _sectionTitle('管理'),
          ListTile(
            key: const Key('settings-hosts-admin'),
            title: const Text('服务器管理'),
            trailing: const Icon(Icons.chevron_right, size: 18),
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => HostsAdminScreen(api: widget.api)),
            ),
          ),
          ListTile(
            key: const Key('settings-shares'),
            title: const Text('分享管理'),
            trailing: const Icon(Icons.chevron_right, size: 18),
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => SharesScreen(api: widget.api, baseUrl: widget.baseUrl)),
            ),
          ),
          _sectionTitle('修改密码'),
          ExpansionTile(
            key: const Key('settings-password-tile'),
            title: const Text('修改密码', style: TextStyle(fontSize: 14)),
            childrenPadding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
            children: [
              Text('当前来源：$_passwordSource', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
              const SizedBox(height: 10),
              _field('当前密码', TextField(key: const Key('settings-pw-current'), controller: _pwCurrent, obscureText: true)),
              _field('新密码', TextField(key: const Key('settings-pw-new'), controller: _pwNew, obscureText: true)),
              _field('确认新密码', TextField(key: const Key('settings-pw-confirm'), controller: _pwConfirm, obscureText: true)),
              FilledButton(
                key: const Key('settings-pw-submit'),
                onPressed: _pwBusy ? null : _changePassword,
                child: const Text('修改密码'),
              ),
              _status(_pwStatus, good: _pwStatus.startsWith('密码已更新')),
            ],
          ),
          _sectionTitle('导入 / 导出'),
          ExpansionTile(
            key: const Key('settings-io-tile'),
            title: const Text('导入 / 导出', style: TextStyle(fontSize: 14)),
            childrenPadding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
            children: [
              Text('含服务器凭据、分窗布局与预设。先设一个导出密码，文件用 AES-GCM 加密，密码只在本地使用、不会上传。',
                  style: TextStyle(fontSize: 12, color: AppTheme.muted)),
              const SizedBox(height: 10),
              _field('导出密码',
                  TextField(key: const Key('settings-export-password'), controller: _exportPassword, obscureText: true)),
              Wrap(
                spacing: 8,
                children: [
                  OutlinedButton(
                    key: const Key('settings-export-hosts'),
                    onPressed: _exportBusy ? null : () => _export('hosts'),
                    child: const Text('导出服务器'),
                  ),
                  OutlinedButton(
                    key: const Key('settings-export-grid'),
                    onPressed: _exportBusy ? null : () => _export('grid'),
                    child: const Text('导出分窗布局'),
                  ),
                  OutlinedButton(
                    key: const Key('settings-export-all'),
                    onPressed: _exportBusy ? null : () => _export('all'),
                    child: const Text('全部导出'),
                  ),
                ],
              ),
              _status(_exportStatus, good: _exportStatus.startsWith('已加密导出')),
              const Divider(height: 28),
              Row(
                children: [
                  OutlinedButton(
                    key: const Key('settings-import-pick'),
                    onPressed: _pickImportFile,
                    child: const Text('选择文件'),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(_importFileName ?? '未选择文件',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(fontSize: 12, color: AppTheme.muted)),
                  ),
                ],
              ),
              const SizedBox(height: 10),
              _field('文件密码',
                  TextField(key: const Key('settings-import-password'), controller: _importPassword, obscureText: true)),
              FilledButton(
                key: const Key('settings-import-run'),
                onPressed: _importBusy ? null : _import,
                child: const Text('导入'),
              ),
              _status(_importStatus, good: _importStatus.startsWith('主机：') || _importStatus.startsWith('分窗')),
              for (final failure in _importFailures)
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text(failure, style: TextStyle(fontSize: 11.5, color: AppTheme.danger)),
                ),
            ],
          ),
          _sectionTitle('插件', plugins == null ? null : (plugins.isEmpty ? '没有安装插件' : '$enabledCount / ${plugins.length} 已启用')),
          ExpansionTile(
            key: const Key('settings-plugins-tile'),
            title: const Text('插件', style: TextStyle(fontSize: 14)),
            childrenPadding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
            children: [
              if (_pluginsError != null)
                Text('读取插件失败：$_pluginsError', style: TextStyle(fontSize: 12, color: AppTheme.danger))
              else if (plugins == null)
                const Center(child: CircularProgressIndicator(strokeWidth: 2))
              else if (plugins.isEmpty)
                Text('把插件放到 plugins/<名字>/ 里，重启后就能在这里开关。',
                    style: TextStyle(fontSize: 12, color: AppTheme.muted))
              else
                for (final plugin in plugins) _pluginRow(plugin),
            ],
          ),
          _sectionTitle('主题', current.label),
          ExpansionTile(
            key: const Key('settings-themes-tile'),
            title: const Text('主题', style: TextStyle(fontSize: 14)),
            childrenPadding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
            children: [
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final palette in AppThemes.all)
                    TextButton(
                      key: Key('theme-${palette.name}'),
                      onPressed: () => _selectTheme(palette),
                      style: TextButton.styleFrom(
                        side: BorderSide(
                          color: palette.name == current.name ? palette.accent : AppTheme.line,
                          width: palette.name == current.name ? 2 : 1,
                        ),
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                      ),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Container(
                            width: 16,
                            height: 16,
                            decoration: BoxDecoration(
                              color: palette.page,
                              shape: BoxShape.circle,
                              border: Border.all(color: palette.accent, width: 2),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Text(palette.label),
                          if (palette.name == current.name) ...[
                            const SizedBox(width: 4),
                            const Icon(Icons.check, size: 16),
                          ],
                        ],
                      ),
                    ),
                ],
              ),
              const SizedBox(height: 6),
              Text('主题记在账号上，换设备也会跟着走。', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
            ],
          ),
          const SizedBox(height: 16),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: OutlinedButton(
              key: const Key('settings-logout'),
              style: OutlinedButton.styleFrom(foregroundColor: AppTheme.danger),
              onPressed: _logout,
              child: const Text('退出登录'),
            ),
          ),
        ],
      ),
    );
  }

  Widget _pluginRow(Map<String, dynamic> plugin) {
    final name = '${plugin['name'] ?? ''}';
    final enabled = plugin['enabled'] == true;
    final error = '${plugin['error'] ?? ''}';
    final loaded = plugin['loaded'] == true;
    final rowError = _pluginErrors[name];
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(12, 10, 8, 10),
      decoration: BoxDecoration(
        color: enabled ? AppTheme.card : AppTheme.page,
        border: Border.all(color: AppTheme.line),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Flexible(child: Text(name, style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13))),
                    const SizedBox(width: 6),
                    Text('${plugin['version'] ?? ''}',
                        style: TextStyle(fontSize: 10.5, color: AppTheme.faint, fontFamily: 'monospace')),
                  ],
                ),
                if ('${plugin['description'] ?? ''}'.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 3),
                    child: Text('${plugin['description']}',
                        style: TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                  ),
                if (error.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 3),
                    child: Text(error, style: TextStyle(fontSize: 11.5, color: AppTheme.danger)),
                  ),
                if (rowError != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 3),
                    child: Text(rowError, style: TextStyle(fontSize: 11.5, color: AppTheme.danger)),
                  ),
              ],
            ),
          ),
          Switch(
            key: Key('plugin-switch-$name'),
            value: enabled,
            // Web disables the switch for a plugin that failed to load.
            onChanged: (error.isNotEmpty && !loaded) ? null : (value) => _togglePlugin(plugin, value),
          ),
        ],
      ),
    );
  }
}
