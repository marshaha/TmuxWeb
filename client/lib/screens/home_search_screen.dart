/// Board search: the Flutter equivalent of the search box on web/home.html.
/// Web runs the search twice (names first with messages=0, then with
/// messages=1); this screen makes the single messages=1 call, which is the
/// same result set with one round trip instead of two.
library;

import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../models/home_board.dart';
import '../models/models.dart';
import '../theme.dart';
import 'chat_screen.dart';

String _s(Object? value) => value == null ? '' : '$value';

Pane _paneFromSearch(Map<String, dynamic> item) {
  final pane = _s(item['pane']).isNotEmpty ? _s(item['pane']) : _s(item['id']);
  return paneFromBoardRef(pane,
      title: _s(item['title']), session: _s(item['session']), provider: _s(item['provider']).isEmpty ? null : _s(item['provider']));
}

class HomeSearchScreen extends StatefulWidget {
  const HomeSearchScreen({super.key, required this.api});

  final Api api;

  @override
  State<HomeSearchScreen> createState() => _HomeSearchScreenState();
}

class _HomeSearchScreenState extends State<HomeSearchScreen> {
  final _query = TextEditingController();
  HomeSearchResults? _results;
  String? _error;
  bool _loading = false;
  bool _searched = false;

  @override
  void dispose() {
    _query.dispose();
    super.dispose();
  }

  Future<void> _search() async {
    final query = _query.text.trim();
    if (query.isEmpty) return;
    setState(() {
      _loading = true;
      _error = null;
      _searched = true;
    });
    try {
      final results = HomeSearchResults.fromJson(await widget.api.homeSearch(query));
      if (!mounted) return;
      setState(() {
        _results = results;
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

  void _openPane(Pane pane) {
    final api = widget.api;
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: api is TerminalApi ? api as TerminalApi : null,
      ),
    ));
  }

  Widget _sectionTitle(String title, int count) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 20, 16, 8),
      child: Text('$title $count', style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600)),
    );
  }

  @override
  Widget build(BuildContext context) {
    final results = _results;
    return Scaffold(
      appBar: AppBar(title: const Text('搜索')),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.all(16),
            child: SizedBox(
              height: 56,
              child: Row(
                children: [
                  Expanded(
                    child: TextField(
                      key: const Key('search-query'),
                      controller: _query,
                      autocorrect: false,
                      textInputAction: TextInputAction.search,
                      onSubmitted: (_) => _search(),
                      decoration: const InputDecoration(hintText: '搜索服务器 / session / pane（支持正则），以及最近对话内容…'),
                    ),
                  ),
                  const SizedBox(width: 8),
                  // A bare FilledButton in this Row gets unbounded constraints
                  // from the Column above and tries to size to infinity;
                  // pin it so the row lays out on every platform.
                  SizedBox(
                    width: 72,
                    height: 48,
                    child: FilledButton(key: const Key('search-submit'), onPressed: _search, child: const Text('搜索')),
                  ),
                ],
              ),
            ),
          ),
          if (_loading) const LinearProgressIndicator(),
          Expanded(
            child: _error != null
                ? ListView(
                    padding: const EdgeInsets.all(24),
                    children: [
                      Text(_error!, style: const TextStyle(color: Color(0xFFB34B43), fontSize: 13)),
                      const SizedBox(height: 12),
                      OutlinedButton(onPressed: _search, child: const Text('重试')),
                    ],
                  )
                : !_searched
                    ? const Padding(
                        padding: EdgeInsets.all(24),
                        child: Text('先匹配主机 / session / pane，再看最近的对话。', style: TextStyle(color: AppTheme.muted)),
                      )
                    : results == null
                        ? const SizedBox.shrink()
                        : ListView(
                            padding: const EdgeInsets.only(bottom: 16),
                            children: [
                              if (results.mode.isNotEmpty)
                                Padding(
                                  padding: const EdgeInsets.symmetric(horizontal: 16),
                                  child: Text('匹配方式：${results.mode}', style: const TextStyle(fontSize: 11.5, color: AppTheme.faint)),
                                ),
                              _sectionTitle('主机', results.hosts.length),
                              for (final host in results.hosts)
                                ListTile(
                                  title: Text(_s(host['name'])),
                                  subtitle: Text(
                                      [_s(host['hostname']), '${_s(host['panes'])} panes']
                                          .where((part) => part.isNotEmpty)
                                          .join(' · '),
                                      style: const TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                                ),
                              _sectionTitle('会话', results.sessions.length),
                              for (final session in results.sessions)
                                ListTile(
                                  title: Text(_s(session['session'])),
                                  subtitle: Text(
                                      [_s(session['host']), '${_s(session['panes'])} panes']
                                          .where((part) => part.isNotEmpty)
                                          .join(' · '),
                                      style: const TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                                ),
                              _sectionTitle('会话 pane', results.panes.length),
                              for (final pane in results.panes)
                                ListTile(
                                  title: Text(_s(pane['title']).isNotEmpty ? _s(pane['title']) : _s(pane['id'])),
                                  subtitle: Text(
                                      [_s(pane['host_name']), _s(pane['session']), _s(pane['provider'])]
                                          .where((part) => part.isNotEmpty)
                                          .join(' · '),
                                      style: const TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                                  onTap: () => _openPane(_paneFromSearch(pane)),
                                ),
                              _sectionTitle('对话', results.messages.length),
                              for (final message in results.messages)
                                ListTile(
                                  title: Text(_s(message['snippet']).isNotEmpty ? _s(message['snippet']) : _s(message['title']),
                                      maxLines: 3, overflow: TextOverflow.ellipsis),
                                  subtitle: Text(
                                      [_s(message['host']), _s(message['session']), _s(message['role'])]
                                          .where((part) => part.isNotEmpty)
                                          .join(' · '),
                                      style: const TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                                  onTap: () => _openPane(_paneFromSearch(message)),
                                ),
                              _sectionTitle('看板卡片', results.cards.length),
                              for (final card in results.cards)
                                ListTile(
                                  title: Text(_s(card['title'])),
                                  subtitle: Text(_s(card['body']), maxLines: 3, overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(fontSize: 12, color: AppTheme.muted)),
                                ),
                              if (results.hosts.isEmpty &&
                                  results.sessions.isEmpty &&
                                  results.panes.isEmpty &&
                                  results.messages.isEmpty &&
                                  results.cards.isEmpty)
                                const Padding(
                                  padding: EdgeInsets.all(24),
                                  child: Text('没有匹配的结果。', style: TextStyle(color: AppTheme.muted)),
                                ),
                            ],
                          ),
          ),
        ],
      ),
    );
  }
}
