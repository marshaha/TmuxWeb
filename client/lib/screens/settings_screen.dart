import 'dart:convert';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/api_client.dart';
import '../models/grid_state.dart';
import '../services/config_crypto.dart';
import '../services/save_file.dart';
import '../services/config_import.dart';
import '../theme.dart';
import '../widgets/pane_hover_preview.dart';
import 'hosts_admin_screen.dart';
import 'shares_screen.dart';
import 'login_screen.dart';

/// Per-pane sidebar-view storage scaffolding, mirroring web's
/// localStorage['tmux-files-view'] pattern: pane ref → view, most
/// recent last, capped at 60 panes (oldest trimmed first).
///
/// OPEN DELTA (phase 2): Flutter has no file panel inside the sidebar
/// at all — files are a separate page — so no view state has anything
/// to attach to yet and nothing writes through this class. The settings
/// toggle below only syncs the preference itself (sidebar: pane|none);
/// claiming per-pane memory complete would be false until the sidebar
/// file panel exists.
class SidebarViewMemory {
  static const String key = 'tmux-files-view';
  static const int maxPanes = 60;

  static Future<Map<String, String>> load() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(key);
      if (raw == null || raw.isEmpty) return {};
      final data = jsonDecode(raw);
      if (data is! Map) return {};
      return {
        for (final entry in data.entries)
          if (entry.key is String && entry.value is String) entry.key as String: entry.value as String,
      };
    } catch (_) {
      return {};
    }
  }

  static Future<void> save(String paneRef, String view) async {
    if (paneRef.isEmpty) return;
    try {
      final prefs = await SharedPreferences.getInstance();
      final views = await load();
      views.remove(paneRef);
      views[paneRef] = view;
      while (views.length > maxPanes) {
        views.remove(views.keys.first);
      }
      await prefs.setString(key, jsonEncode(views));
    } catch (_) {}
  }

  static Future<void> forget(String paneRef) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final views = await load();
      if (views.remove(paneRef) != null) {
        await prefs.setString(key, jsonEncode(views));
      }
    } catch (_) {}
  }
}

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
    _loadDisplayPrefs();
  }

  // Hover-preview delay (web settings.js loadPreviewPref /
  // savePreviewDelay) and sidebar memory (toggleSidebarMemory): local
  // mirrors in SharedPreferences, synced to the account via updatePrefs.
  String _previewDelay = '2';
  bool _sidebarMemory = false;
  String _prefsStatus = '';

  static const _previewValues = ['off', '1', '2', '3'];
  static const _sidebarPrefKey = 'tmux-sidebar-memory';

  Future<void> _loadDisplayPrefs() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final localPreview = prefs.getString(PaneHoverPreview.delayKey);
      final localSidebar = prefs.getString(_sidebarPrefKey);
      if (!mounted) return;
      setState(() {
        if (localPreview != null && _previewValues.contains(localPreview)) _previewDelay = localPreview;
        _sidebarMemory = localSidebar == 'pane';
      });
    } catch (_) {}
    try {
      final data = await widget.api.prefs();
      final stored = data['prefs'];
      if (stored is! Map || !mounted) return;
      final serverPreview = '${stored['preview'] ?? ''}';
      final serverSidebar = '${stored['sidebar'] ?? ''}';
      setState(() {
        if (_previewValues.contains(serverPreview)) _previewDelay = serverPreview;
        if (serverSidebar == 'pane' || serverSidebar == 'none') _sidebarMemory = serverSidebar == 'pane';
      });
      final prefs = await SharedPreferences.getInstance();
      if (_previewValues.contains(serverPreview)) {
        await prefs.setString(PaneHoverPreview.delayKey, serverPreview);
      }
      if (serverSidebar == 'pane' || serverSidebar == 'none') {
        await prefs.setString(_sidebarPrefKey, serverSidebar);
      }
    } catch (_) {
      // Offline: the local mirrors are what hover and the sidebar read.
    }
  }

  Future<void> _savePreviewDelay(String value) async {
    if (!_previewValues.contains(value)) value = '2';
    setState(() {
      _previewDelay = value;
      _prefsStatus = '';
    });
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(PaneHoverPreview.delayKey, value);
    } catch (_) {}
    try {
      await widget.api.updatePrefs({'preview': value});
    } catch (_) {
      if (mounted) setState(() => _prefsStatus = '预览延迟已记在本机，同步到账号失败');
    }
  }

  Future<void> _toggleSidebarMemory(bool next) async {
    setState(() {
      _sidebarMemory = next;
      _prefsStatus = '';
    });
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_sidebarPrefKey, next ? 'pane' : 'none');
    } catch (_) {}
    try {
      await widget.api.updatePrefs({'sidebar': next ? 'pane' : 'none'});
    } catch (_) {
      if (mounted) setState(() => _prefsStatus = '侧栏记忆已记在本机，同步到账号失败');
    }
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
        // The grid page IS ported: GridScreen persists its layout and
        // presets under GridState.gridKey / presetsKey, and the export
        // reads those same keys (null when nothing was stored, like web).
        final prefs = await SharedPreferences.getInstance();
        bundle['grid'] = _storedJson(prefs, GridState.gridKey);
        bundle['grid_presets'] = _storedJson(prefs, GridState.presetsKey);
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
    try {
      final picked = await FilePicker.pickFiles();
      if (picked.isEmpty) return;
      final bytes = await picked.first.xFile.readAsBytes();
      if (!mounted) return;
      setState(() {
        _importFileName = picked.first.name;
        _importBytes = bytes;
      });
    } catch (error) {
      // 选择器或读盘失败以前会变成一个没人看的异步异常，界面什么都不显示。
      if (!mounted) return;
      setState(() => _importStatus = '读取文件失败：$error');
    }
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
              _field('新密码',
                  TextField(key: const Key('settings-pw-new'), controller: _pwNew, obscureText: true, decoration: const InputDecoration(hintText: '至少 6 位'))),
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
          _sectionTitle('悬停预览与侧栏'),
          ExpansionTile(
            key: const Key('settings-display-tile'),
            title: const Text('悬停预览与侧栏', style: TextStyle(fontSize: 14)),
            childrenPadding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
            children: [
              Row(
                children: [
                  const Expanded(child: Text('悬停预览延迟', style: TextStyle(fontSize: 13))),
                  DropdownButton<String>(
                    key: const Key('settings-preview-delay'),
                    value: _previewDelay,
                    items: const [
                      DropdownMenuItem(value: 'off', child: Text('关闭')),
                      DropdownMenuItem(value: '1', child: Text('1 秒')),
                      DropdownMenuItem(value: '2', child: Text('2 秒')),
                      DropdownMenuItem(value: '3', child: Text('3 秒')),
                    ],
                    onChanged: (value) {
                      if (value != null) _savePreviewDelay(value);
                    },
                  ),
                ],
              ),
              Text('鼠标在侧栏或主页卡片上停多久，才弹出最后一条消息。触屏没有悬停，不会触发。',
                  style: TextStyle(fontSize: 12, color: AppTheme.muted)),
              const SizedBox(height: 10),
              SwitchListTile(
                key: const Key('settings-sidebar-memory'),
                title: const Text('记住每个 pane 的侧栏视图', style: TextStyle(fontSize: 13)),
                subtitle: Text('开启后每个 pane 记住自己上次的侧栏视图；侧栏文件面板仍在开发中，暂无视图可记。',
                    style: TextStyle(fontSize: 12, color: AppTheme.muted)),
                contentPadding: EdgeInsets.zero,
                value: _sidebarMemory,
                onChanged: _toggleSidebarMemory,
              ),
              _status(_prefsStatus),
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
