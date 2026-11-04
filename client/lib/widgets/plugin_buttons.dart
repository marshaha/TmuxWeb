import 'package:flutter/material.dart';

import '../api/api_client.dart';
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
/// Loading and failure match web/assets/plugins-ui.js exactly: web
/// renders nothing until the fetch succeeds, and on a failed fetch it
/// silently mounts nothing (non-ok → return, exception → swallowed) —
/// no loading text, no retry affordance. A transient failure therefore
/// just leaves the row empty until the widget is mounted again.
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
      });
    } catch (_) {
      // Web parity: silently drop the buttons on fetch failure — no
      // error text, no retry affordance. An empty list renders nothing,
      // exactly like web mounting nothing.
      if (!mounted) return;
      setState(() => _buttons = const []);
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
    final buttons = _buttons;
    // Web parity: nothing while loading, nothing on failure, nothing
    // when the list is empty — no '加载中…' text, no retry affordance.
    if (buttons == null || buttons.isEmpty) return const SizedBox.shrink();
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
