import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../theme.dart';
import '../screens/plugin_page_screen.dart';

/// Sidebar buttons contributed by enabled plugins, the Flutter equivalent
/// of web/assets/plugins-ui.js: fetch /api/plugins' `buttons` array
/// ({label, href, icon, title, plugin}) and render one button per entry.
/// Tapping a button opens [PluginPageScreen] with the href resolved
/// against the plugin-assets base.
///
/// Web renders these as icon buttons reusing its SVG sprite; Flutter has
/// no equivalent sprite for the plugins' icon names, so a button with a
/// non-empty icon field gets a generic extension glyph and its label —
/// the icon name itself is not a Flutter icon and is never interpreted
/// as one.
///
/// Not mounted into any shell yet (phase-2 integration): the coordinator
/// wires this into the console/board/grid shells.
class PluginButtons extends StatefulWidget {
  const PluginButtons({
    super.key,
    this.api,
    this.buttonsLoader,
    required this.baseUrl,
    this.cookieHeader = '',
    this.createPlatformView = true,
  }) : assert(api != null || buttonsLoader != null, 'PluginButtons needs api or buttonsLoader');

  /// API client for [Api.pluginButtons]. Optional when [buttonsLoader]
  /// is given (tests inject a loader instead of a full Api).
  final Api? api;

  /// Test seam / override for the button fetch. Production passes null
  /// and the widget calls [Api.pluginButtons].
  final Future<List<Map<String, dynamic>>> Function()? buttonsLoader;

  final String baseUrl;

  /// `Cookie` header value forwarded to [PluginPageScreen] for native
  /// WebView cookie injection (phase-2 passes the API client's header).
  final String cookieHeader;

  /// Forwarded to [PluginPageScreen]; false in tests so navigation never
  /// creates a platform view.
  final bool createPlatformView;

  @override
  State<PluginButtons> createState() => _PluginButtonsState();
}

class _PluginButtonsState extends State<PluginButtons> {
  List<Map<String, dynamic>>? _buttons;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final loader = widget.buttonsLoader ?? widget.api!.pluginButtons;
      final buttons = await loader();
      if (!mounted) return;
      setState(() {
        _buttons = buttons.where((button) => '${button['label'] ?? ''}'.isNotEmpty).toList();
        _error = null;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = '$error');
    }
  }

  void _open(Map<String, dynamic> button) {
    final href = '${button['href'] ?? ''}';
    if (href.isEmpty) return;
    final plugin = '${button['plugin'] ?? ''}';
    final label = '${button['label'] ?? ''}';
    final title = '${button['title'] ?? ''}';
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => PluginPageScreen(
          baseUrl: widget.baseUrl,
          pluginName: plugin,
          entryPath: pluginEntryPath(href),
          title: title.isEmpty ? label : title,
          cookieHeader: widget.cookieHeader,
          createPlatformView: widget.createPlatformView,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_error != null) {
      // Web silently drops sidebar buttons on fetch failure; Flutter
      // shows a small retry affordance instead of failing silently, so a
      // broken plugin API is visible rather than mysteriously absent.
      return InkWell(
        onTap: _load,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
          child: Text('插件按钮加载失败，点击重试', style: TextStyle(fontSize: 11, color: AppTheme.danger)),
        ),
      );
    }
    final buttons = _buttons;
    if (buttons == null) {
      // Static text, not a spinner: an indeterminate progress widget
      // would never settle under pumpAndSettle in widget tests.
      return const Padding(
        padding: EdgeInsets.symmetric(horizontal: 8, vertical: 6),
        child: Text('加载中…', style: TextStyle(fontSize: 11)),
      );
    }
    if (buttons.isEmpty) return const SizedBox.shrink();
    return Wrap(
      spacing: 6,
      runSpacing: 6,
      children: [
        for (final button in buttons)
          Tooltip(
            message: '${button['title'] ?? ''}'.isEmpty ? '${button['label'] ?? ''}' : '${button['title']}',
            child: OutlinedButton(
              onPressed: () => _open(button),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if ('${button['icon'] ?? ''}'.isNotEmpty) ...[
                    const Icon(Icons.extension, size: 14),
                    const SizedBox(width: 4),
                  ],
                  Text('${button['label'] ?? ''}', style: const TextStyle(fontSize: 12)),
                ],
              ),
            ),
          ),
      ],
    );
  }
}
