/// Board search: the Flutter equivalent of the search box on web/home.html.
///
/// Web behaviour, followed here:
/// - Names first (messages=0, instant), then conversations (messages=1);
///   a newer query cancels an older one (web's seq guard).
/// - Input debounces 280ms; an empty query clears the results.
/// - Mode line: mode note + "先匹配主机 / session / pane，再看最近的对话"
///   + extras (indexing / messages pending / messages partial).
/// - Hosts/sessions/panes/messages sections always render, with web's
///   empty texts when they have no hits; cards only render when non-empty.
/// - A search hit opens the console directly (web's hits are not .card
///   elements, so they navigate, unlike board cards which preview).
library;

import 'dart:async';

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
  const HomeSearchScreen({super.key, required this.api, this.baseUrl = ''});

  final Api api;
  final String baseUrl;

  @override
  State<HomeSearchScreen> createState() => _HomeSearchScreenState();
}

class _HomeSearchScreenState extends State<HomeSearchScreen> {
  final _query = TextEditingController();
  HomeSearchResults? _results;
  String? _error;
  bool _loadingNames = false;
  bool _loadingMessages = false;
  bool _searched = false;
  int _seq = 0;
  Timer? _debounce;

  @override
  void dispose() {
    _query.dispose();
    _debounce?.cancel();
    super.dispose();
  }

  void _onQueryChanged(String value) {
    // Rebuild so the clear button appears/disappears with the text.
    setState(() {});
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 280), _search);
  }

  Future<void> _search() async {
    _debounce?.cancel();
    final query = _query.text.trim();
    if (query.isEmpty) {
      setState(() {
        _results = null;
        _error = null;
        _searched = false;
        _loadingNames = false;
        _loadingMessages = false;
      });
      return;
    }
    final seq = ++_seq;
    setState(() {
      _loadingNames = true;
      _error = null;
      _searched = true;
    });
    try {
      final names = HomeSearchResults.fromJson(await widget.api.homeSearch(query, messages: false));
      if (!mounted || seq != _seq) return;
      setState(() {
        _results = names;
        _loadingNames = false;
        _loadingMessages = true;
      });
    } catch (error) {
      if (!mounted || seq != _seq) return;
      setState(() {
        _error = '$error';
        _loadingNames = false;
        _loadingMessages = false;
      });
      return;
    }
    try {
      final full = HomeSearchResults.fromJson(await widget.api.homeSearch(query, messages: true));
      if (!mounted || seq != _seq) return;
      setState(() {
        _results = full;
        _loadingMessages = false;
      });
    } catch (error) {
      // Names stay on screen; only a toast, like web's catch in search().
      if (!mounted || seq != _seq) return;
      setState(() => _loadingMessages = false);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
    }
  }

  void _openPane(Pane pane) {
    final api = widget.api;
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => ChatScreen(
        source: PaneChatSource(api: api, pane: pane),
        terminalApi: api is TerminalApi ? api as TerminalApi : null,
        baseUrl: widget.baseUrl,
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
                      onChanged: _onQueryChanged,
                      onSubmitted: (_) => _search(),
                      decoration: InputDecoration(
                        hintText: '搜索服务器 / session / pane（支持正则），以及最近对话内容…',
                        suffixIcon: _query.text.isEmpty
                            ? null
                            : IconButton(
                                key: const Key('search-clear'),
                                tooltip: '清除',
                                icon: const Icon(Icons.clear, size: 18),
                                onPressed: () {
                                  _query.clear();
                                  _search();
                                },
                              ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  SizedBox(
                    width: 72,
                    height: 48,
                    child: FilledButton(key: const Key('search-submit'), onPressed: _search, child: const Text('搜索')),
                  ),
                ],
              ),
            ),
          ),
          if (_loadingNames || _loadingMessages) const LinearProgressIndicator(),
          Expanded(
            child: _error != null
                ? ListView(
                    padding: const EdgeInsets.all(24),
                    children: [
                      Text(_error!, style: TextStyle(color: AppTheme.danger, fontSize: 13)),
                      const SizedBox(height: 12),
                      OutlinedButton(onPressed: _search, child: const Text('重试')),
                    ],
                  )
                : !_searched
                    ? Padding(
                        padding: const EdgeInsets.all(24),
                        child: Text('先匹配主机 / session / pane，再看最近的对话。', style: TextStyle(color: AppTheme.muted)),
                      )
                    : results == null
                        ? const SizedBox.shrink()
                        : ListView(
                            padding: const EdgeInsets.only(bottom: 16),
                            children: [
                              Padding(
                                padding: const EdgeInsets.symmetric(horizontal: 16),
                                child: Text(
                                  '匹配方式：${results.modeNote} · 先匹配主机 / session / pane，再看最近的对话${results.extras.isNotEmpty ? ' · ${results.extras}' : ''}',
                                  style: TextStyle(fontSize: 11.5, color: AppTheme.faint),
                                ),
                              ),
                              _sectionTitle('服务器', results.hosts.length),
                              if (results.hosts.isEmpty)
                                Padding(
                                    padding: const EdgeInsets.symmetric(horizontal: 16),
                                    child: Text('没有命中的服务器', style: TextStyle(color: AppTheme.muted)))
                              else
                                for (final host in results.hosts)
                                  ListTile(
                                    title: Text(_s(host['name'])),
                                    subtitle: Text(
                                        [_s(host['hostname']), _s(host['port']), '${_s(host['panes'])} panes']
                                            .where((part) => part.isNotEmpty)
                                            .join(' · '),
                                        style: TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                                  ),
                              _sectionTitle('tmux session', results.sessions.length),
                              if (results.sessions.isEmpty)
                                Padding(
                                    padding: const EdgeInsets.symmetric(horizontal: 16),
                                    child: Text('没有命中的 session', style: TextStyle(color: AppTheme.muted)))
                              else
                                for (final session in results.sessions)
                                  ListTile(
                                    title: Text(_s(session['session'])),
                                    subtitle: Text(
                                        [_s(session['host']), '${_s(session['panes'])} panes']
                                            .where((part) => part.isNotEmpty)
                                            .join(' · '),
                                        style: TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                                  ),
                              _sectionTitle('pane', results.panes.length),
                              if (results.panes.isEmpty)
                                Padding(
                                    padding: const EdgeInsets.symmetric(horizontal: 16),
                                    child: Text('没有命中的 pane', style: TextStyle(color: AppTheme.muted)))
                              else
                                for (final pane in results.panes)
                                  ListTile(
                                    leading: Container(
                                      width: 7,
                                      height: 7,
                                      decoration: BoxDecoration(
                                          shape: BoxShape.circle,
                                          color: _s(pane['status']) == 'busy'
                                              ? AppTheme.accent
                                              : (_s(pane['status']) == 'idle' ? AppTheme.green : AppTheme.faint)),
                                    ),
                                    title: Text(
                                        [_s(pane['title']), _s(pane['target']), _s(pane['id'])]
                                            .firstWhere((part) => part.isNotEmpty, orElse: () => ''),
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis),
                                    subtitle: Column(
                                      crossAxisAlignment: CrossAxisAlignment.start,
                                      children: [
                                        Text(
                                            [_s(pane['host_name']), _s(pane['session']), _s(pane['provider'])]
                                                .where((part) => part.isNotEmpty)
                                                .join(' · '),
                                            style: TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                                        if (_s(pane['path']).isNotEmpty)
                                          Text(_s(pane['path']),
                                              maxLines: 1,
                                              overflow: TextOverflow.ellipsis,
                                              style: TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                                      ],
                                    ),
                                    onTap: () => _openPane(_paneFromSearch(pane)),
                                  ),
                              _sectionTitle('对话内容', results.messages.length),
                              if (results.messages.isEmpty)
                                Padding(
                                    padding: const EdgeInsets.symmetric(horizontal: 16),
                                    child: Text('最近对话里没有命中', style: TextStyle(color: AppTheme.muted)))
                              else
                                for (final message in results.messages)
                                  ListTile(
                                    title: Text(_s(message['snippet']).isNotEmpty ? _s(message['snippet']) : _s(message['title']),
                                        maxLines: 3, overflow: TextOverflow.ellipsis),
                                    subtitle: Text(
                                        [_s(message['role']), _s(message['host']), _s(message['session'])]
                                            .where((part) => part.isNotEmpty)
                                            .join(' · '),
                                        style: TextStyle(fontSize: 11.5, color: AppTheme.muted)),
                                    onTap: () => _openPane(_paneFromSearch(message)),
                                  ),
                              if (results.cards.isNotEmpty) ...[
                                _sectionTitle('看板卡片', results.cards.length),
                                for (final card in results.cards)
                                  ListTile(
                                    title: Text(_s(card['title'])),
                                    subtitle: Text(_s(card['body']), maxLines: 3, overflow: TextOverflow.ellipsis,
                                        style: TextStyle(fontSize: 12, color: AppTheme.muted)),
                                  ),
                              ],
                            ],
                          ),
          ),
        ],
      ),
    );
  }
}
