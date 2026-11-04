/// Full preview page: the Flutter equivalent of web/preview.html, and of
/// the iframe inside web/home.html's right-side preview drawer.
///
/// It is a full chat view for one pane — transcript + composer, so it can
/// talk, not just read — with the preview page's own header: back to the
/// board, the pane title, a favorite star, a terminal entry, and a button
/// to open the same pane in the console chat. It is deliberately a
/// different screen from ChatScreen: ChatScreen is the console view, this
/// is the preview view, and the header is what tells them apart.
library;

import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../models/models.dart';
import '../widgets/favorite_star_button.dart';
import 'chat_screen.dart';
import 'grid_screen.dart';
import 'hosts_screen.dart';
import 'terminal_screen.dart';

class PanePreviewScreen extends StatelessWidget {
  const PanePreviewScreen({super.key, required this.api, required this.pane, this.terminalApi, this.baseUrl = ''});

  final Api api;
  final Pane pane;
  final TerminalApi? terminalApi;
  final String baseUrl;

  void _openChat(BuildContext context) {
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: terminalApi,
        baseUrl: baseUrl,
      ),
    ));
  }

  /// Web preview.html's header, in order: back, favorite, terminal,
  /// grid, reload — and deliberately NO pause toggle (web preview
  /// polls at 2s/15s with no pause control; the embedded-chat pause
  /// toggle belongs to another surface).
  ///
  /// Polling itself lives in the embedded ChatScreen: this page
  /// passes 2s, and the embedded chat switches to web's 15s hidden
  /// interval on app-lifecycle backgrounding (visible 2s / hidden
  /// 15s, like web preview.html's document-visibility rule).
  void _openGrid(BuildContext context) {
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => GridScreen(api: api, baseUrl: baseUrl),
    ));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(pane.label, maxLines: 1, overflow: TextOverflow.ellipsis),
            const Text('对话预览', style: TextStyle(fontSize: 11, fontWeight: FontWeight.w400)),
          ],
        ),
        actions: [
          FavoriteStarButton(key: const Key('preview-fav'), api: api, paneRef: pane.ref),
          if (terminalApi != null)
            IconButton(
              key: const Key('preview-terminal'),
              tooltip: '终端',
              icon: const Icon(Icons.terminal, size: 20),
              onPressed: () => Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => TerminalScreen(
                  api: terminalApi!,
                  pane: pane,
                  onOpenInConsole: () => Navigator.of(context).push(
                      MaterialPageRoute(
                          builder: (_) => HostsScreen(
                              api: api,
                              baseUrl: baseUrl,
                              initialPaneRef: pane.ref))),
                ),
              )),
            ),
          IconButton(
            key: const Key('preview-open-chat'),
            tooltip: '在对话中打开',
            icon: const Icon(Icons.chat_bubble_outline, size: 20),
            onPressed: () => _openChat(context),
          ),
          IconButton(
            key: const Key('preview-grid'),
            tooltip: '加到多窗口',
            icon: const Icon(Icons.grid_view_outlined, size: 20),
            onPressed: () => _openGrid(context),
          ),
          IconButton(
            key: const Key('preview-reload'),
            tooltip: '刷新',
            icon: const Icon(Icons.refresh, size: 20),
            // The embedded ChatScreen owns polling; reload rebuilds
            // this page so the embedded view restarts its refresh
            // from the cached transcript, like web's reload button.
            onPressed: () {
              Navigator.of(context).pushReplacement(MaterialPageRoute(
                builder: (_) => PanePreviewScreen(api: api, pane: pane, terminalApi: terminalApi, baseUrl: baseUrl),
              ));
            },
          ),
        ],
      ),
      body: ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: terminalApi,
        embedded: true,
        baseUrl: baseUrl,
        // Web preview.html polls 2s while visible (15s hidden); the
        // embedded chat switches to 15s on lifecycle backgrounding,
        // so this 2s visible cadence matches web exactly.
        poll: const Duration(seconds: 2),
      ),
    );
  }
}
