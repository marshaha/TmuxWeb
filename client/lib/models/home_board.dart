/// Models for GET /api/home and GET /api/home/search, mirroring the shapes
/// tmux_web.py builds in build_home_payload / home_search (main 3fff9a1).
/// Web behaviour kept: favorite title falls back to default_title, then
/// pane_title, then the pane reference; a favorite preview is the last
/// message (preview, else agent_preview), truncated to 400 chars server-side.
library;

import 'models.dart';

String _s(Object? value) => value == null ? '' : '$value';

Map<String, dynamic> _map(Object? value) =>
    value is Map ? Map<String, dynamic>.from(value) : const <String, dynamic>{};

List<Map<String, dynamic>> _maps(Object? value) =>
    value is List ? value.whereType<Map>().map((item) => Map<String, dynamic>.from(item)).toList() : const [];

String _hostIdOf(String paneRef) => paneRef.contains('~') ? paneRef.split('~').first : 'localhost';

Pane paneFromBoardRef(String paneRef,
    {String title = '', String session = '', String window = '', String path = '', String? provider, String command = ''}) {
  return Pane(
    ref: paneRef,
    title: title,
    command: command,
    hostId: _hostIdOf(paneRef),
    session: session,
    window: window,
    path: path,
    provider: provider,
    hasTranscript: provider != null && provider.isNotEmpty,
  );
}

/// The web hover panel's preview for one pane, following home.html's
/// cardPreview: the last agent turn first, then the last message, then a
/// hint. `offline`/`cached` come from the board entry, not the preview.
class HoverPreview {
  HoverPreview({
    required this.role,
    required this.text,
    this.timestamp = 0,
    this.truncated = false,
    this.offline = false,
    this.cached = false,
  });

  final String role;
  final String text;
  final double timestamp;
  final bool truncated;
  final bool offline;
  final bool cached;
}

class HomeFavorite {
  HomeFavorite({
    required this.pane,
    this.note = '',
    this.status = '',
    this.provider,
    this.title = '',
    this.defaultTitle = '',
    this.customTitle = false,
    this.paneTitle = '',
    this.path = '',
    this.session = '',
    this.window = '',
    this.host = '',
    this.command = '',
    this.previewRole = '',
    this.previewText = '',
    this.previewTimestamp = 0,
    this.agentPreviewRole = '',
    this.agentPreviewText = '',
    this.agentPreviewTimestamp = 0,
    this.agentPreviewTruncated = false,
    this.cached = false,
    this.cachedAt = 0,
    this.waiting = false,
    this.pending = false,
  });

  final String pane;
  final String note;
  final String status;
  final String? provider;
  final String title;
  final String defaultTitle;
  final bool customTitle;
  final String paneTitle;
  final String path;
  final String session;
  final String window;
  final String host;
  final String command;

  /// The card's own preview: the last message (web's card() uses preview,
  /// never agent_preview).
  final String previewRole;
  final String previewText;
  final double previewTimestamp;

  /// The hover panel's preview: the last agent turn.
  final String agentPreviewRole;
  final String agentPreviewText;
  final double agentPreviewTimestamp;
  final bool agentPreviewTruncated;
  final bool cached;
  final double cachedAt;
  final bool waiting;
  final bool pending;

  /// Web cardPreview for this favorite.
  HoverPreview get hoverPreview {
    final offline = status == 'offline';
    if (agentPreviewText.isNotEmpty) {
      return HoverPreview(
          role: agentPreviewRole, text: agentPreviewText, timestamp: agentPreviewTimestamp, truncated: agentPreviewTruncated, offline: offline, cached: cached);
    }
    if (previewText.isNotEmpty) {
      return HoverPreview(role: previewRole, text: previewText, timestamp: previewTimestamp, offline: offline, cached: cached);
    }
    return HoverPreview(
        role: provider ?? 'pane',
        text: cached ? '缓存里也没有这个会话的内容。' : '这个会话暂时没有可预览的内容。',
        offline: offline,
        cached: cached);
  }

  String get displayTitle {
    if (title.isNotEmpty) return title;
    if (defaultTitle.isNotEmpty) return defaultTitle;
    if (paneTitle.isNotEmpty) return paneTitle;
    return pane;
  }

  String get meta {
    final parts = [host, session, window].where((part) => part.isNotEmpty).join(' · ');
    return parts.isNotEmpty ? parts : (path.isNotEmpty ? path : pane);
  }

  Pane get asPane => paneFromBoardRef(pane,
      title: displayTitle, session: session, window: window, path: path, provider: provider, command: command);

  factory HomeFavorite.fromJson(Map<String, dynamic> json) {
    final preview = _map(json['preview']);
    final agentPreview = _map(json['agent_preview']);
    return HomeFavorite(
      pane: _s(json['pane']),
      note: _s(json['note']),
      status: _s(json['status']),
      provider: json['provider'] == null ? null : _s(json['provider']),
      title: _s(json['title']),
      defaultTitle: _s(json['default_title']),
      customTitle: json['custom_title'] == true,
      paneTitle: _s(json['pane_title']),
      path: _s(json['path']),
      session: _s(json['session']),
      window: _s(json['window']),
      host: _s(json['host']),
      command: _s(json['command']),
      previewRole: _s(preview['role']),
      previewText: _s(preview['text']),
      previewTimestamp: (preview['timestamp'] as num?)?.toDouble() ?? 0,
      agentPreviewRole: _s(agentPreview['role']),
      agentPreviewText: _s(agentPreview['text']),
      agentPreviewTimestamp: (agentPreview['timestamp'] as num?)?.toDouble() ?? 0,
      agentPreviewTruncated: agentPreview['truncated'] == true,
      cached: json['cached'] == true,
      cachedAt: (json['cached_at'] as num?)?.toDouble() ?? 0,
      waiting: json['waiting'] == true,
      pending: json['pending'] == true,
    );
  }
}

class HomeRecent {
  HomeRecent({
    required this.pane,
    this.visitedAt = 0,
    this.title = '',
    this.host = '',
    this.session = '',
    this.window = '',
    this.provider = '',
    this.status = '',
    this.online = false,
  });

  final String pane;
  final double visitedAt;
  final String title;
  final String host;
  final String session;
  final String window;
  final String provider;
  final String status;
  final bool online;

  String get meta {
    final parts = [host, session, window].where((part) => part.isNotEmpty).join(' · ');
    return parts.isNotEmpty ? parts : pane;
  }

  Pane get asPane => paneFromBoardRef(pane,
      title: title.isNotEmpty ? title : pane, session: session, window: window, provider: provider.isEmpty ? null : provider);

  factory HomeRecent.fromJson(Map<String, dynamic> json) {
    return HomeRecent(
      pane: _s(json['pane']),
      visitedAt: (json['visited_at'] as num?)?.toDouble() ?? 0,
      title: _s(json['title']),
      host: _s(json['host']),
      session: _s(json['session']),
      window: _s(json['window']),
      provider: _s(json['provider']),
      status: _s(json['status']),
      online: json['online'] == true,
    );
  }
}

class HomeCard {
  HomeCard({required this.id, this.title = '', this.body = '', this.paneId = '', this.kind = 'note', this.createdAt = 0});

  final String id;
  final String title;
  final String body;
  final String paneId;
  final String kind;
  final double createdAt;

  Pane? get asPane => paneId.isEmpty
      ? null
      : paneFromBoardRef(paneId, title: title);

  factory HomeCard.fromJson(Map<String, dynamic> json) {
    return HomeCard(
      id: _s(json['id']),
      title: _s(json['title']),
      body: _s(json['body']),
      paneId: _s(json['pane_id']),
      kind: _s(json['kind']).isEmpty ? 'note' : _s(json['kind']),
      createdAt: (json['created_at'] as num?)?.toDouble() ?? 0,
    );
  }
}

class HomeStats {
  HomeStats({this.panes = 0, this.busy = 0, this.idle = 0, this.favorites = 0, this.cards = 0});

  final int panes;
  final int busy;
  final int idle;
  final int favorites;
  final int cards;

  factory HomeStats.fromJson(Map<String, dynamic> json) {
    return HomeStats(
      panes: (json['panes'] as num?)?.toInt() ?? 0,
      busy: (json['busy'] as num?)?.toInt() ?? 0,
      idle: (json['idle'] as num?)?.toInt() ?? 0,
      favorites: (json['favorites'] as num?)?.toInt() ?? 0,
      cards: (json['cards'] as num?)?.toInt() ?? 0,
    );
  }
}

class HomeBoard {
  HomeBoard({required this.favorites, required this.recent, required this.cards, required this.stats});

  final List<HomeFavorite> favorites;
  final List<HomeRecent> recent;
  final List<HomeCard> cards;
  final HomeStats stats;

  factory HomeBoard.fromJson(Map<String, dynamic> json) {
    return HomeBoard(
      favorites: _maps(json['favorites']).map(HomeFavorite.fromJson).toList(),
      recent: _maps(json['recent']).map(HomeRecent.fromJson).toList(),
      cards: _maps(json['cards']).map(HomeCard.fromJson).toList(),
      stats: HomeStats.fromJson(_map(json['stats'])),
    );
  }

  /// Web cardPreview for any board pane: a favorite's hover preview when
  /// the pane is favorited, otherwise the hint (recent entries carry no
  /// preview of their own, exactly like web).
  HoverPreview previewFor(String paneRef, {String? provider, bool offline = false, bool cached = false}) {
    for (final favorite in favorites) {
      if (favorite.pane == paneRef) return favorite.hoverPreview;
    }
    return HoverPreview(
        role: provider ?? 'pane',
        text: cached ? '缓存里也没有这个会话的内容。' : '这个会话暂时没有可预览的内容。',
        offline: offline,
        cached: cached);
  }
}

/// One grouped search result set from GET /api/home/search.
class HomeSearchResults {
  HomeSearchResults({
    this.query = '',
    this.mode = '',
    this.hosts = const [],
    this.sessions = const [],
    this.panes = const [],
    this.messages = const [],
    this.cards = const [],
    this.messagesPending = false,
    this.messagesPartial = false,
    this.indexing = false,
  });

  final String query;
  final String mode;
  final List<Map<String, dynamic>> hosts;
  final List<Map<String, dynamic>> sessions;
  final List<Map<String, dynamic>> panes;
  final List<Map<String, dynamic>> messages;
  final List<Map<String, dynamic>> cards;
  final bool messagesPending;
  final bool messagesPartial;
  final bool indexing;

  /// Web's modeNote in renderSearch.
  String get modeNote => switch (mode) {
        'regex' => '正则',
        'literal-fallback' => '正则无效，已按字面匹配',
        'literal' => '字面匹配',
        _ => mode,
      };

  /// Web's extras line in renderSearch.
  String get extras {
    final parts = <String>[
      if (indexing) '会话索引正在建立，几秒后再搜一次',
      if (messagesPending) '对话搜索中…'
      else if (messagesPartial) '对话还在扫，结果可能不全',
    ];
    return parts.join(' · ');
  }

  factory HomeSearchResults.fromJson(Map<String, dynamic> json) {
    return HomeSearchResults(
      query: _s(json['query']),
      mode: _s(json['mode']),
      hosts: _maps(json['hosts']),
      sessions: _maps(json['sessions']),
      panes: _maps(json['panes']),
      messages: _maps(json['messages']),
      cards: _maps(json['cards']),
      messagesPending: json['messages_pending'] == true,
      messagesPartial: json['messages_partial'] == true,
      indexing: json['indexing'] == true,
    );
  }
}
