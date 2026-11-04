import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/screens/home_board_screen.dart';
import 'package:tmuxweb_client/screens/home_search_screen.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart';

import 'screens_test.dart' show FakeApi, wrap;

/// Same rich tree as the grid sidebar tests: server order, mixed
/// window shapes, one pane per state class.
class _HomeSidebarApi extends FakeApi {
  Map<String, String> states = {'%7': 'idle', '%8': 'busy'};
  Map<String, dynamic> pinsData = {
    'recent': [
      {'pane': 'abc~%0', 'title': 'agent', 'visited_at': 0},
    ],
    'pinned': [],
  };
  final Set<String> favs = {'abc~%0'};

  @override
  Future<List<Host>> hosts({bool refresh = false}) async => [
        Host(id: 'localhost', name: 'localhost', kind: 'local', status: 'online', panes: [
          Pane(ref: '%7', title: 'review', command: 'codex', session: 'codex', window: '0', windowName: 'main', index: '1', number: 7, path: '/srv/work', active: true, hasTranscript: true),
          Pane(ref: '%8', title: '', command: 'bash', session: 'codex', window: '0', windowName: 'main', index: '2', path: '', hasTranscript: true),
          Pane(ref: '%9', title: 'other', command: 'vim', session: 'ops', window: '1', windowName: '', index: '0', number: 3, path: '/tmp', hasTranscript: true),
        ]),
        Host(id: 'abc', name: 'ctyun', kind: 'ssh', status: 'online', panes: [
          Pane(ref: 'abc~%0', title: 'agent', command: 'hermes', session: 'main', window: '0', windowName: '', index: '0', hostId: 'abc', hasTranscript: true),
        ]),
      ];

  @override
  Future<Map<String, String>> paneStates() async => states;

  @override
  Future<Map<String, dynamic>> pins({int limit = 12}) async => pinsData;

  @override
  Future<Map<String, dynamic>> homeFavorites() async => {'favorites': favs.toList(), 'notes': {}};

  @override
  Future<void> addHomeFavorite(String paneRef, {String note = '', String title = ''}) async {
    await super.addHomeFavorite(paneRef, note: note, title: title);
    favs.add(paneRef);
  }

  @override
  Future<void> deleteHomeFavorite(String paneRef) async {
    await super.deleteHomeFavorite(paneRef);
    favs.remove(paneRef);
  }
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  Future<_HomeSidebarApi> pumpHome(WidgetTester tester) async {
    final api = _HomeSidebarApi();
    await tester.pumpWidget(wrap(HomeBoardScreen(api: api, baseUrl: 'http://chat.local:8899')));
    await tester.pumpAndSettle();
    return api;
  }

  Future<void> openDrawer(WidgetTester tester) async {
    tester.state<ScaffoldState>(find.byType(Scaffold).first).openDrawer();
    await tester.pumpAndSettle();
  }

  testWidgets('narrow drawer header carries the six home actions + side-count', (tester) async {
    await pumpHome(tester);
    await openDrawer(tester);

    expect(find.text('4 panes'), findsOneWidget);
    final count = tester.widget<Tooltip>(find.ancestor(
      of: find.text('4 panes'),
      matching: find.byType(Tooltip),
    ).first);
    expect(count.message, '2 台主机 · 4 个 pane');
    // Home peer button is 多窗口 (home.html grid link), not 主页.
    expect(tester.widget<IconButton>(find.byKey(const Key('home-side-console'))).tooltip, '聊天（控制台）');
    expect(tester.widget<IconButton>(find.byKey(const Key('home-side-grid'))).tooltip, '多窗口');
    expect(tester.widget<IconButton>(find.byKey(const Key('home-side-hosts'))).tooltip, '远程主机');
    expect(tester.widget<IconButton>(find.byKey(const Key('home-side-shares'))).tooltip, '管理分享');
    expect(tester.widget<IconButton>(find.byKey(const Key('home-side-reload'))).tooltip, '刷新');
    expect(tester.widget<IconButton>(find.byKey(const Key('home-side-collapse'))).tooltip, '折叠侧边栏');
    // Fixed-bottom recents with grip, like web's shell mount.
    expect(find.byKey(const Key('recent-grip')), findsOneWidget);
    expect(find.byKey(const Key('recent-block')), findsOneWidget);
    // Home has no footer: no grid footer hint, no settings entry.
    expect(find.text('点会话即加入窗口'), findsNothing);
    expect(find.byKey(const Key('home-sidebar-settings')), findsNothing);
  });

  testWidgets('home tree click navigates to the console with that pane', (tester) async {
    await pumpHome(tester);
    await openDrawer(tester);
    await tester.tap(find.byKey(const Key('home-tree-expand-all')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('home-tree-pane-%7')));
    for (var i = 0; i < 20; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    expect(find.byType(HostsScreen), findsOneWidget);
  });

  testWidgets('home tree star toggles with a toast and flips the icon', (tester) async {
    final api = await pumpHome(tester);
    await openDrawer(tester);
    await tester.tap(find.byKey(const Key('home-tree-expand-all')));
    await tester.pumpAndSettle();
    await tester.dragUntilVisible(
      find.byKey(const Key('home-tree-star-%8')),
      find.byKey(const Key('home-tree-list')),
      const Offset(0, -100),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('home-tree-star-%8')));
    await tester.pump();
    expect(find.text('已收藏到主页'), findsOneWidget);
    await tester.pumpAndSettle();
    expect(api.addedFavorites, contains('%8'));
    expect(tester.widget<IconButton>(find.byKey(const Key('home-tree-star-%8'))).tooltip, '取消收藏');
  });

  testWidgets('home sidebar recent row navigates to the console', (tester) async {
    await pumpHome(tester);
    await openDrawer(tester);

    // The board's own recent section shows the same label behind
    // the drawer; scope to the sidebar's recent block.
    await tester.tap(find.descendant(
      of: find.byKey(const Key('recent-block')),
      matching: find.text('agent'),
    ));
    for (var i = 0; i < 20; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    expect(find.byType(HostsScreen), findsOneWidget);
  });

  testWidgets('Escape closes the narrow drawer', (tester) async {
    await pumpHome(tester);
    await openDrawer(tester);
    // Focus a sidebar button first: Escape bubbles from the focused
    // node up to the screen's Focus handler.
    await tester.tap(find.byKey(const Key('home-tree-expand-all')));
    await tester.pumpAndSettle();

    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pumpAndSettle();
    expect(tester.state<ScaffoldState>(find.byType(Scaffold).first).isDrawerOpen, isFalse);
  });

  testWidgets('wide layout shows a resizable column; collapse hides it, side-open restores it',
      (tester) async {
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await pumpHome(tester);

    expect(find.byKey(const Key('home-tree-list')), findsOneWidget);
    expect(find.byKey(const Key('shell-sidebar-resizer')), findsOneWidget);
    expect(find.byKey(const Key('home-side-open')), findsNothing);

    await tester.tap(find.byKey(const Key('home-side-collapse')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('home-tree-list')), findsNothing);
    expect(find.byKey(const Key('home-side-open')), findsOneWidget);
    expect(tester.widget<IconButton>(find.byKey(const Key('home-side-open'))).tooltip, '显示会话列表');

    await tester.tap(find.byKey(const Key('home-side-open')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('home-tree-list')), findsOneWidget);
  });

  testWidgets('home tree load failure renders the error inside the tree', (tester) async {
    final api = _FailingHomeHostsApi();
    await tester.pumpWidget(wrap(HomeBoardScreen(api: api, baseUrl: 'http://chat.local:8899')));
    await tester.pumpAndSettle();
    await openDrawer(tester);
    expect(find.byKey(const Key('home-tree-error')), findsOneWidget);
    expect(find.text('还没有会话'), findsNothing);
  });

  testWidgets('search field Escape clears the query', (tester) async {
    await tester.pumpWidget(wrap(HomeSearchScreen(api: FakeApi(), baseUrl: 'http://chat.local:8899')));
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('search-query')), 'codex');
    await tester.pump(const Duration(milliseconds: 100));
    expect(find.byKey(const Key('search-clear')), findsOneWidget);

    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('search-clear')), findsNothing);
    expect(find.text('先匹配主机 / session / pane，再看最近的对话。'), findsOneWidget);
  });
}

class _FailingHomeHostsApi extends FakeApi {
  @override
  Future<List<Host>> hosts({bool refresh = false}) async => throw Exception('boom');
}
