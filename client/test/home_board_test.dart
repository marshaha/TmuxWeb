import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/models/home_board.dart';

void main() {
  test('favorite title falls back through default and pane titles', () {
    final withTitle = HomeFavorite.fromJson({'pane': '%7', 'title': '自定义', 'default_title': '自动', 'pane_title': '原始'});
    expect(withTitle.displayTitle, '自定义');
    final withDefault = HomeFavorite.fromJson({'pane': '%7', 'default_title': '自动', 'pane_title': '原始'});
    expect(withDefault.displayTitle, '自动');
    final bare = HomeFavorite.fromJson({'pane': '%7'});
    expect(bare.displayTitle, '%7');
    expect(bare.asPane.ref, '%7');
    expect(bare.asPane.hostId, 'localhost');
    final remote = HomeFavorite.fromJson({'pane': 'abc~%3', 'session': 'main'});
    expect(remote.asPane.hostId, 'abc');
    expect(remote.asPane.session, 'main');
  });

  test('favorite preview prefers preview over agent_preview', () {
    final entry = HomeFavorite.fromJson({
      'pane': '%7',
      'preview': {'role': 'assistant', 'text': '最新'},
      'agent_preview': {'role': 'agent', 'text': '旧的'},
      'cached': true,
    });
    expect(entry.previewRole, 'assistant');
    expect(entry.previewText, '最新');
    expect(entry.cached, isTrue);
    final agentOnly = HomeFavorite.fromJson({
      'pane': '%7',
      'agent_preview': {'role': 'agent', 'text': '旧的'},
    });
    expect(agentOnly.previewText, '旧的');
  });

  test('board parses favorites, recent, cards and stats', () {
    final board = HomeBoard.fromJson({
      'favorites': [
        {'pane': '%7', 'title': 'review', 'status': 'idle', 'provider': 'codex'},
      ],
      'recent': [
        {'pane': 'abc~%0', 'title': 'agent', 'host': 'ctyun', 'session': 'main', 'status': 'busy', 'online': true},
      ],
      'cards': [
        {'id': 'c1', 'title': '结论', 'body': '正文', 'pane_id': '%7', 'kind': 'note'},
      ],
      'stats': {'panes': 2, 'busy': 1, 'idle': 1, 'favorites': 1, 'cards': 1},
    });
    expect(board.favorites.single.displayTitle, 'review');
    expect(board.recent.single.meta, contains('ctyun'));
    expect(board.cards.single.asPane?.ref, '%7');
    expect(board.stats.busy, 1);
    expect(board.stats.cards, 1);
    expect(HomeCard.fromJson({'id': 'x', 'title': 't'}).asPane, isNull);
  });

  test('search results group hosts, sessions, panes, messages and cards', () {
    final results = HomeSearchResults.fromJson({
      'query': 'codex',
      'mode': 'literal',
      'hosts': [
        {'id': 'localhost', 'name': 'localhost'},
      ],
      'sessions': [
        {'session': 'codex', 'host': 'localhost'},
      ],
      'panes': [
        {'id': '%7', 'title': 'review'},
      ],
      'messages': [
        {'pane': '%7', 'snippet': '已完成'},
      ],
      'cards': [
        {'title': '结论', 'body': '正文'},
      ],
    });
    expect(results.mode, 'literal');
    expect(results.hosts.single['id'], 'localhost');
    expect(results.panes.single['id'], '%7');
    expect(results.messages.single['snippet'], '已完成');
  });
}
