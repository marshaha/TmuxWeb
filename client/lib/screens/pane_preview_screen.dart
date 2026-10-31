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
import 'terminal_screen.dart';

class PanePreviewScreen extends StatelessWidget {
  const PanePreviewScreen({super.key, required this.api, required this.pane, this.terminalApi});

  final Api api;
  final Pane pane;
  final TerminalApi? terminalApi;

  void _openChat(BuildContext context) {
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: terminalApi,
      ),
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
                builder: (_) => TerminalScreen(api: terminalApi!, pane: pane),
              )),
            ),
          IconButton(
            key: const Key('preview-open-chat'),
            tooltip: '在对话中打开',
            icon: const Icon(Icons.chat_bubble_outline, size: 20),
            onPressed: () => _openChat(context),
          ),
        ],
      ),
      body: ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: terminalApi,
        embedded: true,
      ),
    );
  }
}
