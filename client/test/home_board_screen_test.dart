import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/api/api_client.dart';
import 'package:tmuxweb_client/screens/home_board_screen.dart';
import 'package:tmuxweb_client/screens/home_search_screen.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart';
import 'package:tmuxweb_client/widgets/recent_block.dart';

import 'screens_test.dart' show FakeApi, wrap;

const _baseUrl = 'http://chat.local:8899';

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('board renders stats, favorites, recent and cards', (tester) async {
    await tester.pumpWidget(wrap(HomeBoardScreen(api: FakeApi(), baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    expect(find.text('2 panes'), findsOneWidget);
    expect(find.text('1 busy'), findsOneWidget);
    expect(find.text('review'), findsOneWidget);
    expect(find.textContaining('已完成第一版'), findsOneWidget);
    expect(find.text('agent'), findsOneWidget);
    expect(find.text('结论'), findsOneWidget);
    expect(find.text('先做 API 再做界面'), findsOneWidget);
  });

  testWidgets('board card opens the preview panel, then the chat', (tester) async {
    await tester.pumpWidget(wrap(HomeBoardScreen(api: FakeApi(), baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    await tester.tap(find.text('review').first);
    await tester.pumpAndSettle();
    expect(find.text('完整预览'), findsOneWidget);
    expect(find.text('聊天'), findsOneWidget);
    expect(find.text('关闭'), findsOneWidget);
    expect(find.text('agent 回复'), findsOneWidget);
    expect(find.textContaining('已完成第一版'), findsWidgets);

    await tester.tap(find.text('聊天'));
    await tester.pumpAndSettle();
    expect(find.text('来自 %7 的回复'), findsOneWidget);
  });

  testWidgets('full preview on a narrow screen opens the preview page', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(HomeBoardScreen(api: api, baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    await tester.tap(find.text('review').first);
    await tester.pumpAndSettle();
    await tester.tap(find.text('完整预览'));
    await tester.pumpAndSettle();

    // The preview page can talk: it shows the transcript + composer,
    // and it is a different screen from the console chat.
    expect(find.text('对话预览'), findsOneWidget);
    expect(find.text('来自 %7 的回复'), findsOneWidget);
    expect(find.byKey(const Key('preview-fav')), findsOneWidget);
    expect(find.byKey(const Key('preview-open-chat')), findsOneWidget);

    await tester.tap(find.byKey(const Key('preview-fav')));
    await tester.pumpAndSettle();
    expect(api.addedFavorites, ['%7']);
  });

  testWidgets('full preview on a wide screen opens the right-side panel', (tester) async {
    tester.view.physicalSize = const Size(1600, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(wrap(HomeBoardScreen(api: FakeApi(), baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    // Wide: the shell sidebar tree shows the same pane title, so
    // scope to the board itself (its list lives in a
    // RefreshIndicator; the sidebar has none).
    await tester.tap(find.descendant(
      of: find.byType(RefreshIndicator),
      matching: find.text('review'),
    ).first);
    await tester.pumpAndSettle();
    await tester.tap(find.text('完整预览'));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('preview-panel')), findsOneWidget);
    expect(find.text('来自 %7 的回复'), findsOneWidget);
    expect(find.byKey(const Key('preview-panel-close')), findsOneWidget);

    await tester.tap(find.byKey(const Key('preview-panel-close')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('preview-panel')), findsNothing);
  });

  testWidgets('recent card previews with the hint when it has no preview', (tester) async {
    await tester.pumpWidget(wrap(HomeBoardScreen(api: FakeApi(), baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    await tester.tap(find.text('agent'));
    await tester.pumpAndSettle();
    expect(find.text('这个会话暂时没有可预览的内容。'), findsOneWidget);
    expect(find.text('完整预览'), findsOneWidget);
    expect(find.text('聊天'), findsOneWidget);
  });

  testWidgets('unfavorite, rename and card delete call the board API', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(HomeBoardScreen(api: api, baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('unfav-%7')));
    await tester.pumpAndSettle();
    expect(api.unfavorited, ['%7']);

    await tester.tap(find.byKey(const Key('rename-%7')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('rename-title')), '新标题');
    await tester.tap(find.text('保存'));
    await tester.pumpAndSettle();
    expect(api.renamedFavorites, ['%7|新标题']);

    await tester.ensureVisible(find.byKey(const Key('delete-card-c1')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('delete-card-c1')));
    await tester.pumpAndSettle();
    expect(api.deletedCards, ['c1']);
  });

  testWidgets('cached board stays on screen when the live load fails', (tester) async {
    SharedPreferences.setMockInitialValues({
      'tmux-home-cache-v1:$_baseUrl': jsonEncode({
        'favorites': [
          {'pane': '%9', 'title': '缓存里的收藏', 'status': 'idle', 'provider': 'codex'},
        ],
        'recent': [],
        'cards': [],
        'stats': {'panes': 1, 'busy': 0, 'idle': 1, 'favorites': 1, 'cards': 0},
      }),
    });
    await tester.pumpWidget(wrap(HomeBoardScreen(
        api: FakeApi(boardError: ApiException(500, 'server down')), baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    expect(find.text('缓存里的收藏'), findsOneWidget);
    expect(find.text('显示缓存，正在更新…'), findsOneWidget);
    // No full-screen error with a retry button: the cached board stays.
    expect(find.text('重试'), findsNothing);
  });

  testWidgets('search renders grouped results and clears on empty query', (tester) async {
    await tester.pumpWidget(wrap(HomeSearchScreen(api: FakeApi())));
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('search-query')), 'codex');
    await tester.tap(find.byKey(const Key('search-submit')));
    await tester.pumpAndSettle();

    expect(find.textContaining('匹配方式：字面匹配'), findsOneWidget);
    expect(find.text('localhost'), findsWidgets);
    expect(find.text('review'), findsWidgets);
    expect(find.text('已完成第一版'), findsOneWidget);
    await tester.drag(find.byType(ListView).first, const Offset(0, -800));
    await tester.pumpAndSettle();
    expect(find.text('结论'), findsOneWidget);

    await tester.tap(find.byKey(const Key('search-clear')));
    await tester.pumpAndSettle();
    expect(find.text('先匹配主机 / session / pane，再看最近的对话。'), findsOneWidget);
    expect(find.text('已完成第一版'), findsNothing);
  });

  testWidgets('hosts screen opens the board', (tester) async {
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    // Narrow console: the drawer brand row carries the same
    // home-board key as the AppBar — tap the AppBar copy.
    await tester.tap(find.descendant(
      of: find.byType(AppBar),
      matching: find.byKey(const Key('home-board')),
    ));
    await tester.pumpAndSettle();
    expect(find.text('主页板'), findsWidgets);
    expect(find.text('review'), findsOneWidget);
  });

  testWidgets('hosts tree star toggles the favorite set', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(HostsScreen(api: api, baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    // Narrow console: the tree lives in a shut, collapsed drawer —
    // open and expand before tapping a pane star.
    await tester.tap(find.byKey(const Key('narrow-menu')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('tree-expand')));
    await tester.pumpAndSettle();
    // The ctyun section can sit below the drawer viewport fold —
    // scroll it into view before tapping its star.
    await tester.ensureVisible(find.byKey(const Key('star-abc~%0')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('star-abc~%0')));
    await tester.pumpAndSettle();
    expect(api.unfavorited, ['abc~%0']);

    await tester.ensureVisible(find.byKey(const Key('star-%7')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('star-%7')));
    await tester.pumpAndSettle();
    expect(api.addedFavorites, ['%7']);
  });

  test('stripMarkdown removes syntax for the 4-line card summaries', () {
    expect(stripMarkdown('## 标题 **粗体** `代码` [链接](https://example.com)'), '标题 粗体 代码 链接');
    expect(stripMarkdown('- 项目\n> 引用\n| a | b |'), contains('项目'));
    expect(stripMarkdown('- 项目\n> 引用\n| a | b |'), isNot(contains('#')));
    expect(stripMarkdown('```dart\ncode();\n```\n正文'), contains('正文'));
    expect(stripMarkdown('```dart\ncode();\n```\n正文'), isNot(contains('code();')));
  });

  testWidgets('home recent rows share the sidebar row actions', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(HomeBoardScreen(api: api, baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    // Shared row: pin + star + the kept delete action, not just delete.
    expect(find.byKey(const Key('recent-pin-abc~%0')), findsOneWidget);
    expect(find.byKey(const Key('recent-star-abc~%0')), findsOneWidget);
    expect(find.byKey(const Key('delete-recent-abc~%0')), findsOneWidget);

    await tester.tap(find.byKey(const Key('recent-pin-abc~%0')));
    await tester.pumpAndSettle();
    expect(api.pinnedCalls, contains('abc~%0:true'));

    await tester.tap(find.byKey(const Key('delete-recent-abc~%0')));
    await tester.pumpAndSettle();
    expect(api.deletedRecent, ['abc~%0']);
  });

  testWidgets('wide preview drawer covers the full viewport above the bar', (tester) async {
    tester.view.physicalSize = const Size(1600, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(wrap(HomeBoardScreen(api: FakeApi(), baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    // Wide: the shell sidebar tree shows the same pane title, so
    // scope to the board itself (its list lives in a
    // RefreshIndicator; the sidebar has none).
    await tester.tap(find.descendant(
      of: find.byType(RefreshIndicator),
      matching: find.text('review'),
    ).first);
    await tester.pumpAndSettle();
    await tester.tap(find.text('完整预览'));
    await tester.pumpAndSettle();

    final panel = find.byKey(const Key('preview-panel'));
    expect(panel, findsOneWidget);
    final rect = tester.getRect(panel);
    // Web: width min(760px, 56vw), full height from top:0.
    expect(rect.width, 760);
    expect(rect.top, 0);
    expect(rect.height, 900);
    final container = tester.widget<Container>(panel);
    final decoration = container.decoration as BoxDecoration;
    final shadow = decoration.boxShadow!.single;
    expect(shadow.offset, const Offset(-16, 0));
    expect(shadow.blurRadius, 48);
  });

  testWidgets('recent block renders pins, actions, rename and collapse', (tester) async {
    SharedPreferences.setMockInitialValues({});
    final pins = <String>[];
    final renames = <String>[];
    final stars = <String>[];
    var opens = <String>[];
    Future<RecentBlockData> load() async => const RecentBlockData(
          pinned: [
            RecentRowData(paneRef: '%7', label: '钉住的会话', pinLabel: '钉住的会话', pinned: true, starred: true),
          ],
          recent: [
            RecentRowData(paneRef: 'abc~%0', label: '最近的会话', visitedLabel: '01-02 03:04', starred: false),
          ],
        );
    await tester.pumpWidget(wrap(Scaffold(
      body: RecentBlock(
        load: load,
        onOpen: (ref) => opens.add(ref),
        onTogglePin: (ref, pinned) async => pins.add('$ref:$pinned'),
        onRenamePin: (ref, label) async => renames.add('$ref:$label'),
        onToggleFavorite: (ref, starred) async => stars.add('$ref:$starred'),
      ),
    )));
    await tester.pumpAndSettle();

    expect(find.text('钉住的会话'), findsOneWidget);
    expect(find.text('最近的会话'), findsOneWidget);
    // Pencil rename exists only on the pinned row.
    expect(find.byKey(const Key('recent-rename-%7')), findsOneWidget);
    expect(find.byKey(const Key('recent-rename-abc~%0')), findsNothing);

    await tester.tap(find.byKey(const Key('recent-pin-abc~%0')));
    await tester.pumpAndSettle();
    expect(pins, ['abc~%0:true']);

    await tester.tap(find.byKey(const Key('recent-rename-%7')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('recent-rename-input-%7')), '新名字');
    // E10 removed the explicit save/cancel buttons: Enter saves.
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();
    expect(renames, ['%7:新名字']);

    await tester.tap(find.byKey(const Key('recent-fold')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('recent-list')), findsNothing);
    await tester.tap(find.byKey(const Key('recent-fold')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('recent-list')), findsOneWidget);

    await tester.tap(find.text('最近的会话'));
    expect(opens, ['abc~%0']);
    expect(stars, isEmpty);
  });
}
