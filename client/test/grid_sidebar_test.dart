import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/screens/grid_screen.dart';
import 'package:tmuxweb_client/screens/settings_screen.dart';

import 'screens_test.dart' show FakeApi, wrap;

/// FakeApi with a rich, multi-host tree: server order (not sorted),
/// mixed window shapes, and one pane per state class.
class _SidebarApi extends FakeApi {
  Map<String, String> states = {'%7': 'idle', '%8': 'busy'};
  Map<String, dynamic> pinsData = {
    'recent': [
      // Must be a host pane: out-of-tree unpinned rows are dropped
      // by the block's knownPaneRefs filter (web recents.js:144-156).
      {'pane': 'abc~%0', 'title': 'agent', 'visited_at': 0},
    ],
    'pinned': [],
  };
  final Set<String> favs = {'%7'};

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
}

Map<String, dynamic> _gridJson(List<String> panes) => {
      'panes': panes,
      'widths': {},
      'layout': 'strip',
      'cols': 2,
      'rows': 2,
      'colFr': [1, 1],
      'rowFr': [1, 1],
      'modes': {},
      'explicit': {},
    };

Future<Map<String, dynamic>> _savedGridJson() async {
  final prefs = await SharedPreferences.getInstance();
  final raw = prefs.getString('tmux-grid-v1');
  return raw == null ? {} : Map<String, dynamic>.from(jsonDecode(raw) as Map);
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  Future<_SidebarApi> pumpGrid(WidgetTester tester, {List<String> saved = const []}) async {
    final api = _SidebarApi();
    SharedPreferences.setMockInitialValues({'tmux-grid-v1': jsonEncode(_gridJson(saved))});
    await tester.pumpWidget(wrap(GridScreen(api: api, baseUrl: 'http://chat.local:8899')));
    await tester.pumpAndSettle();
    return api;
  }

  Future<void> openDrawer(WidgetTester tester) async {
    tester.state<ScaffoldState>(find.byType(Scaffold).first).openDrawer();
    await tester.pumpAndSettle();
  }

  testWidgets('narrow drawer header carries all six web actions + side-count', (tester) async {
    await pumpGrid(tester);
    await openDrawer(tester);

    // Web side-count: panes total, with the hosts/panes tooltip.
    expect(find.text('4 panes'), findsOneWidget);
    final count = tester.widget<Tooltip>(find.ancestor(
      of: find.text('4 panes'),
      matching: find.byType(Tooltip),
    ).first);
    expect(count.message, '2 台主机 · 4 个 pane');
    // Six header actions, web's tooltip texts (grid.html:286-297).
    expect(tester.widget<IconButton>(find.byKey(const Key('grid-drawer-console'))).tooltip, '聊天（控制台）');
    expect(tester.widget<IconButton>(find.byKey(const Key('grid-side-home'))).tooltip, '主页');
    expect(tester.widget<IconButton>(find.byKey(const Key('grid-side-hosts'))).tooltip, '远程主机');
    expect(tester.widget<IconButton>(find.byKey(const Key('grid-side-shares'))).tooltip, '管理分享');
    expect(tester.widget<IconButton>(find.byKey(const Key('grid-side-reload'))).tooltip, '刷新');
    expect(tester.widget<IconButton>(find.byKey(const Key('grid-side-collapse'))).tooltip, '折叠侧边栏');
    // Footer settings entry (grid.html:303).
    expect(find.byKey(const Key('grid-sidebar-settings')), findsOneWidget);
    expect(find.text('设置'), findsOneWidget);
  });

  testWidgets('tree starts collapsed, expands in server order with web labels', (tester) async {
    await pumpGrid(tester);
    await openDrawer(tester);

    // Web default: nothing saved in tmux-tree-open, so all closed.
    expect(find.byKey(const Key('grid-tree-pane-%7')), findsNothing);
    await tester.tap(find.byKey(const Key('grid-tree-expand-all')));
    await tester.pumpAndSettle();

    // Top of the tree (built without scrolling): server order,
    // window label `window: windowName`, #N, target, title, path.
    expect(find.text('localhost'), findsOneWidget);
    expect(find.text('0: main'), findsOneWidget);
    expect(find.byKey(const Key('grid-tree-pane-%7')), findsOneWidget);
    expect(find.text('#7'), findsOneWidget);
    expect(find.text('pane 1 · codex'), findsOneWidget);
    expect(find.text('review'), findsOneWidget);
    expect(find.text('/srv/work'), findsOneWidget);
    expect(tester.widget<IconButton>(find.byKey(const Key('grid-tree-star-%7'))).tooltip, '取消收藏');
    // Below-fold rows need a scroll before they are even built
    // (long lazy ListView): sessions in first-seen order, the
    // unnumbered pane's —, the untitled fallback, the second host.
    await tester.dragUntilVisible(
      find.byKey(const Key('grid-tree-pane-%8')),
      find.byKey(const Key('grid-tree-list')),
      const Offset(0, -100),
    );
    await tester.pumpAndSettle();
    expect(find.text('—'), findsWidgets);
    expect(find.text('(无标题)'), findsOneWidget);
    expect(tester.widget<IconButton>(find.byKey(const Key('grid-tree-star-%8'))).tooltip, '收藏到主页');
    await tester.dragUntilVisible(
      find.text('ops'),
      find.byKey(const Key('grid-tree-list')),
      const Offset(0, -100),
    );
    await tester.pumpAndSettle();
    expect(find.text('ops'), findsOneWidget);
    // Bare '1' also matches count texts; pin the window node by key.
    expect(find.byKey(const Key('grid-tree-node-localhost/ops/1:')), findsOneWidget);
    await tester.dragUntilVisible(
      find.text('ctyun'),
      find.byKey(const Key('grid-tree-list')),
      const Offset(0, -100),
    );
    await tester.pumpAndSettle();
    expect(find.text('ctyun'), findsOneWidget);
    // Collapse-all hides the leaves again (tools row stays put).
    await tester.tap(find.byKey(const Key('grid-tree-collapse-all')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('grid-tree-pane-%7')), findsNothing);
  });

  testWidgets('tree row tap adds the pane and keeps the drawer open', (tester) async {
    await pumpGrid(tester);
    await openDrawer(tester);
    await tester.tap(find.byKey(const Key('grid-tree-expand-all')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('grid-tree-pane-%7')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('grid-remove-%7')), findsOneWidget);
    expect((await _savedGridJson())['panes'], ['%7']);
    // I7: web openPane never collapses the sidebar.
    expect(tester.state<ScaffoldState>(find.byType(Scaffold).first).isDrawerOpen, isTrue);
    expect(find.byKey(const Key('grid-tree-list')), findsOneWidget);
  });

  testWidgets('in-grid rows are marked in the tree', (tester) async {
    await pumpGrid(tester, saved: ['%7']);
    await openDrawer(tester);
    await tester.tap(find.byKey(const Key('grid-tree-expand-all')));
    await tester.pumpAndSettle();

    final leaf = tester.widget<InkWell>(find.byKey(const Key('grid-tree-pane-%7')));
    // The tinted Container is the leaf's child, not an ancestor.
    final container = tester.widget<Container>(find.descendant(
      of: find.byKey(const Key('grid-tree-pane-%7')),
      matching: find.byType(Container),
    ).first);
    expect(leaf, isNotNull);
    expect((container.decoration as BoxDecoration?)?.border, isNotNull);
  });

  testWidgets('tree star toggle calls addHomeFavorite and flips the icon', (tester) async {
    final api = await pumpGrid(tester);
    await openDrawer(tester);
    await tester.tap(find.byKey(const Key('grid-tree-expand-all')));
    await tester.pumpAndSettle();
    await tester.dragUntilVisible(
      find.byKey(const Key('grid-tree-star-%8')),
      find.byKey(const Key('grid-tree-list')),
      const Offset(0, -200),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('grid-tree-star-%8')));
    await tester.pumpAndSettle();

    expect(api.addedFavorites, contains('%8'));
    expect(tester.widget<IconButton>(find.byKey(const Key('grid-tree-star-%8'))).tooltip, '取消收藏');
  });

  testWidgets('recent row tap adds the pane, keeps the drawer open, clear uses the API', (tester) async {
    final api = await pumpGrid(tester);
    await openDrawer(tester);

    // Fixed-bottom mount: grip + block render (I8).
    expect(find.byKey(const Key('recent-grip')), findsOneWidget);
    expect(find.byKey(const Key('recent-block')), findsOneWidget);
    await tester.tap(find.text('agent'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('grid-remove-abc~%0')), findsOneWidget);
    expect(tester.state<ScaffoldState>(find.byType(Scaffold).first).isDrawerOpen, isTrue);

    await tester.tap(find.byKey(const Key('recent-clear')));
    await tester.pumpAndSettle();
    expect(api.clearedUnpinnedCalls, 1);
  });

  testWidgets('Escape closes the narrow drawer', (tester) async {
    await pumpGrid(tester);
    await openDrawer(tester);
    // Focus a sidebar button first: Escape bubbles from the focused
    // node up to the screen's Focus handler.
    await tester.tap(find.byKey(const Key('grid-tree-expand-all')));
    await tester.pumpAndSettle();

    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pumpAndSettle();
    expect(tester.state<ScaffoldState>(find.byType(Scaffold).first).isDrawerOpen, isFalse);
  });

  testWidgets('reload header action toasts 已刷新', (tester) async {
    await pumpGrid(tester);
    await openDrawer(tester);
    await tester.tap(find.byKey(const Key('grid-side-reload')));
    await tester.pump();
    expect(find.text('已刷新'), findsOneWidget);
  });

  testWidgets('wide layout shows a resizable column; collapse hides it, side-open restores it',
      (tester) async {
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await pumpGrid(tester, saved: ['%7']);

    // Wide: sidebar column visible with no drawer interaction, resizer
    // present, side-open hidden (web: open.hidden unless collapsed).
    expect(find.byKey(const Key('grid-tree-list')), findsOneWidget);
    expect(find.byKey(const Key('shell-sidebar-resizer')), findsOneWidget);
    expect(find.byKey(const Key('grid-side-open')), findsNothing);

    await tester.tap(find.byKey(const Key('grid-side-collapse')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('grid-tree-list')), findsNothing);
    expect(find.byKey(const Key('grid-side-open')), findsOneWidget);
    expect(tester.widget<IconButton>(find.byKey(const Key('grid-side-open'))).tooltip, '会话列表');

    await tester.tap(find.byKey(const Key('grid-side-open')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('grid-tree-list')), findsOneWidget);
  });

  testWidgets('wide sidebar width persists under tmux-side-w and dblclick resets to 268',
      (tester) async {
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    SharedPreferences.setMockInitialValues({
      'tmux-grid-v1': jsonEncode(_gridJson(['%7'])),
      'tmux-side-w': 400.0,
    });
    final api = _SidebarApi();
    await tester.pumpWidget(wrap(GridScreen(api: api, baseUrl: 'http://chat.local:8899')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('grid-tree-list')), findsOneWidget);

    // Two quick taps land as a double-tap (same helper shape as
    // hosts_tree_test's double-tap note).
    await tester.tap(find.byKey(const Key('shell-sidebar-resizer')));
    await tester.pump(const Duration(milliseconds: 50));
    await tester.tap(find.byKey(const Key('shell-sidebar-resizer')));
    await tester.pumpAndSettle();
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getDouble('tmux-side-w'), 268);
  });

  testWidgets('footer settings button opens SettingsScreen', (tester) async {
    await pumpGrid(tester);
    await openDrawer(tester);
    await tester.tap(find.byKey(const Key('grid-sidebar-settings')));
    for (var i = 0; i < 20; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    expect(find.byType(SettingsScreen), findsOneWidget);
  });

  testWidgets('tree load failure renders the error inside the tree', (tester) async {
    final api = _FailingHostsApi();
    SharedPreferences.setMockInitialValues({'tmux-grid-v1': jsonEncode(_gridJson([]))});
    await tester.pumpWidget(wrap(GridScreen(api: api, baseUrl: 'http://chat.local:8899')));
    await tester.pumpAndSettle();
    await openDrawer(tester);
    expect(find.byKey(const Key('grid-tree-error')), findsOneWidget);
    expect(find.textContaining('boom'), findsOneWidget);
    expect(find.text('还没有会话'), findsNothing);
  });
}

class _FailingHostsApi extends FakeApi {
  @override
  Future<List<Host>> hosts({bool refresh = false}) async => throw Exception('boom');
}
