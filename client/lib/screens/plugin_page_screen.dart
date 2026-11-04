import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../theme.dart';
import '../widgets/web_content_view.dart';

/// Renders one plugin's static page, the Flutter equivalent of following a
/// web sidebar plugin button (web/assets/plugins-ui.js).
///
/// Plugin assets live at `/plugin-assets/<name>/...` (plugin_api.py,
/// require_auth, disabled plugin → 404). Auth mirrors mermaid: on native
/// the session cookie ([cookieHeader], from the API client's Cookie
/// header) is injected into the WebView before loading; on the Flutter
/// web target the iframe is same-origin so the browser's own cookies
/// apply automatically.
///
/// Not mounted into any shell yet (phase-2 integration): [PluginButtons]
/// navigates here, and the coordinator wires it into the console/board/
/// grid shells.
String pluginEntryPath(String href) {
  final match = RegExp(r'^/plugin-assets/[^/]+/(.*)$').firstMatch(href.trim());
  if (match != null) return match.group(1) ?? '';
  final value = href.trim();
  return value.startsWith('/') ? value.substring(1) : value;
}

/// Resolves a plugin page to its absolute URL. Absolute http(s) hrefs pass
/// through; absolute paths are resolved against [baseUrl]; anything else
/// is resolved against the plugin-assets base for [pluginName].
String pluginPageUrl(String baseUrl, String pluginName, String entryPath) {
  final entry = entryPath.trim();
  if (entry.startsWith('http://') || entry.startsWith('https://')) return entry;
  var base = baseUrl.trim();
  while (base.endsWith('/')) {
    base = base.substring(0, base.length - 1);
  }
  if (entry.startsWith('/')) return '$base$entry';
  return '$base/plugin-assets/$pluginName/$entry';
}

/// Turns a raw WebView load failure into the message the user sees. A 404
/// from /plugin-assets means the plugin is disabled or the page does not
/// exist (the server answers both the same way); a 403 means the plugin
/// did not declare the `assets` permission.
String pluginLoadErrorMessage(String raw) {
  if (raw.contains('404')) return '插件已停用或页面不存在（404）';
  if (raw.contains('403')) return '没有权限查看该插件页面（403）';
  return raw;
}

class PluginPageScreen extends StatefulWidget {
  const PluginPageScreen({
    super.key,
    required this.baseUrl,
    required this.pluginName,
    required this.entryPath,
    this.title = '',
    this.cookieHeader = '',
    this.createPlatformView = true,
  });

  final String baseUrl;
  final String pluginName;

  /// Entry path inside the plugin's static folder, e.g. `index.html`.
  /// Use [pluginEntryPath] to derive this from a sidebar button href.
  final String entryPath;
  final String title;
  final String cookieHeader;

  /// False in tests: renders a placeholder instead of a platform view,
  /// which cannot run under `flutter test`.
  final bool createPlatformView;

  @override
  State<PluginPageScreen> createState() => _PluginPageScreenState();
}

class _PluginPageScreenState extends State<PluginPageScreen> {
  bool _loading = true;
  String? _error;
  int _attempt = 0;

  bool get _isWindows => !kIsWeb && defaultTargetPlatform == TargetPlatform.windows;

  String get _pageTitle => widget.title.isEmpty ? widget.pluginName : widget.title;

  Uri get _pageUri => Uri.parse(pluginPageUrl(widget.baseUrl, widget.pluginName, widget.entryPath));

  void _retry() {
    setState(() {
      _error = null;
      _loading = true;
      _attempt++;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.page,
      appBar: AppBar(
        title: Text(_pageTitle, maxLines: 1, overflow: TextOverflow.ellipsis),
        actions: [
          IconButton(
            key: const Key('plugin-page-close'),
            tooltip: '关闭',
            icon: const Icon(Icons.close, size: 20),
            onPressed: () => Navigator.of(context).maybePop(),
          ),
        ],
      ),
      body: _buildBody(),
    );
  }

  Widget _buildBody() {
    if (_isWindows) {
      // webview_flutter 4.14.1 has no Windows backend: never show an
      // unauthenticated or half-loaded page, show the gap plainly.
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Text(
            '当前平台（Windows）暂不支持内嵌插件页面',
            style: TextStyle(fontSize: 13),
            textAlign: TextAlign.center,
          ),
        ),
      );
    }
    if (_error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.extension_off, size: 32, color: AppTheme.muted),
              const SizedBox(height: 12),
              Text(_error!, style: TextStyle(fontSize: 13, color: AppTheme.muted), textAlign: TextAlign.center),
              const SizedBox(height: 16),
              OutlinedButton(onPressed: _retry, child: const Text('重试')),
            ],
          ),
        ),
      );
    }
    if (!widget.createPlatformView) {
      return const Center(
        key: Key('plugin-page-placeholder'),
        child: Text('插件页面视图', style: TextStyle(fontSize: 12)),
      );
    }
    return Stack(
      children: [
        WebContentView(
          key: ValueKey('plugin-web-$_attempt'),
          url: _pageUri,
          cookies: parseCookieHeader(widget.cookieHeader),
          onLoaded: () {
            if (mounted && _error == null) setState(() => _loading = false);
          },
          onError: (message) {
            if (mounted) {
              setState(() {
                _error = pluginLoadErrorMessage(message);
                _loading = false;
              });
            }
          },
        ),
        if (_loading) const Center(child: CircularProgressIndicator()),
      ],
    );
  }
}
