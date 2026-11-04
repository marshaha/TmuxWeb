import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/api/api_client.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart';
import 'package:tmuxweb_client/theme.dart';

import 'screens_test.dart' show FakeApi, wrap;

/// Console-sidebar parity (web console index.html): tree grouping
/// and row shape, tree-open persistence, brand/footer, host refresh,
/// silent starring, recent-block caller wiring, narrow drawer, and
/// the L4/L5/L6 empty/error/loading states.
///
/// Widget-test rules honored: no awaited async IO before the first
/// pumpWidget; wide tests set 1400x900 with tearDown resets;
/// AppTheme.resetForTest in setUp/tearDown; mock SharedPreferences.
class _ConsoleApi extends FakeApi {
  final List<String> touched = [];
  final List<bool> hostsRefreshCalls = [];
  Map<String, dynamic> pinsData = {'recent': [], 'pinned': []};
  Completer<List<Host>>? deferredHosts;

  _ConsoleApi({super.hostsError});

  @override
  Future<List<Host>> hosts({bool refresh = false}) async {
    hostsRefreshCalls.add(refresh);
    final deferred = deferredHosts;
    if (deferred != null) {
      deferredHosts = null;
      return deferred.future;
    }
    if (hostsError != null) throw hostsError!;
    return _consoleHosts;
  }

  @override
  Future<void> touchHomeRecent(String paneRef) async {
    touched.add(paneRef);
  }

  @override
  Future<Map<String, dynamic>> pins({int limit = 12}) async => pinsData;

  @override
  Future<Map<String, String>> paneStates() async =>
      {'h1~%0': 'busy', 'h1~%1': 'idle'};

  @override
  Future<Map<String, dynamic>> homeFavorites() async =>
      {'favorites': favoritePanes.toList(), 'notes': {}};

  /// Three panes in server order: sessions zeta then alpha (NOT
  /// alphabetical), two panes sharing window 0:dev, one in 1:dev.
  /// Window '0' holds panes %0 (numbered, active) and %1
  /// (unnumbered); the third pane has no state entry (unknown).
  static final List<Host> _consoleHosts = [
    Host(id: 'h1', name: 'workstation', kind: 'local', status: 'online', panes: [
      Pane(ref: 'h1~%0', title: 'review', command: 'codex', session: 'zeta', window: '0', windowName: 'dev', index: '0', path: '/srv/work', active: true, number: 7),
      Pane(ref: 'h1~%1', title: 'shell', command: 'zsh', session: 'zeta', window: '0', windowName: 'dev', index: '1', path: '/srv/work'),
      Pane(ref: 'h1~%2', title: 'logs', command: 'tail', session: 'alpha', window: '1', windowName: 'dev', index: '0', path: '/var/log'),
    ]),
    Host(id: 'h2', name: 'remote', kind: 'ssh', status: 'online', panes: []),
  ];
}

Future<void> _pumpWide(WidgetTester tester, _ConsoleApi api) async {
  SharedPreferences.setMockInitialValues({});
  AppTheme.resetForTest();
  // NOTE: do not pin debugDefaultTargetPlatformOverride here — the
  // framework verifies debug vars before helper-registered tearDowns
  // run, so a helper-set override fails every test. Widget tests
  // default to Android (coarse), so the brand row renders its
  // swipeable form; tests tap brand buttons via ensureVisible, which
  // scrolls them into view (and is a no-op on the fine path).
  tester.view.physicalSize = const Size(1400, 900);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(AppTheme.resetForTest);
  await tester.pumpWidget(wrap(HostsScreen(api: api, baseUrl: 'http://127.0.0.1:8787')));
  await tester.pumpAndSettle();
}

Future<void> _pumpNarrow(WidgetTester tester, _ConsoleApi api) async {
  SharedPreferences.setMockInitialValues({});
  AppTheme.resetForTest();
  addTearDown(AppTheme.resetForTest);
  await tester.pumpWidget(wrap(HostsScreen(api: api, baseUrl: 'http://127.0.0.1:8787')));
  await tester.pumpAndSettle();
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  group('tree grouping and row (A3/A4/A5/A6)', () {
    testWidgets('server order kept, windows grouped by window:window_name, session count is windows',
        (tester) async {
      final api = _ConsoleApi();
      await _pumpWide(tester, api);
      await tester.tap(find.byKey(const Key('tree-expand')));
      await tester.pumpAndSettle();

      // Sessions stay in server order: zeta above alpha, not sorted.
      final zetaTop = tester.getTopLeft(find.text('zeta')).dy;
      final alphaTop = tester.getTopLeft(find.text('alpha')).dy;
      expect(zetaTop, lessThan(alphaTop));
      // One window group for both zeta panes, labelled `0: dev`.
      expect(find.text('0: dev'), findsOneWidget);
      expect(find.text('1: dev'), findsOneWidget);
      // Session count is the number of WINDOWS, not panes.
      expect(find.text('1 windows'), findsNWidgets(2));
      // Both panes of window 0 are reachable under the one group.
      expect(find.byKey(const Key('pane-h1~%0')), findsOneWidget);
      expect(find.byKey(const Key('pane-h1~%1')), findsOneWidget);
    });

    testWidgets('pane row matches web: active dot, #N or —, pane line, state dot, title, path',
        (tester) async {
      final api = _ConsoleApi();
      await _pumpWide(tester, api);
      await tester.tap(find.byKey(const Key('tree-expand')));
      await tester.pumpAndSettle();

      expect(find.text('pane 0 · codex'), findsOneWidget);
      expect(find.text('pane 1 · zsh'), findsOneWidget);
      expect(find.text('#7'), findsOneWidget);
      // Two unnumbered panes (%1, %2) render web's `—`.
      expect(find.text('—'), findsNWidgets(2));
      // 'review' is also the recent row's title — scope to the tile.
      expect(
        find.descendant(
          of: find.byKey(const Key('pane-h1~%0')),
          matching: find.text('review'),
        ),
        findsOneWidget,
      );
      expect(find.text('/srv/work'), findsWidgets);
      // The old ref/provider subtitle is gone.
      expect(find.text('h1~%0 · codex · /srv/work'), findsNothing);
      // State dots: busy amber, idle green, unknown grey.
      BoxDecoration dot(String ref) =>
          tester.widget<Container>(find.byKey(Key('pane-state-$ref'))).decoration as BoxDecoration;
      expect(dot('h1~%0').color, const Color(0xFFD89A3C));
      expect(dot('h1~%1').color, AppTheme.green);
      expect(dot('h1~%2').color, const Color(0xFFCFCCC4));
    });
  });

  group('tree open state (A1/A2)', () {
    testWidgets('expand-all and collapse-all persist under tmux-tree-open', (tester) async {
      final api = _ConsoleApi();
      await _pumpWide(tester, api);

      // Default is all-collapsed (web starts from an empty set).
      expect(find.byKey(const Key('host-h1-shut')), findsOneWidget);
      await tester.tap(find.byKey(const Key('tree-expand')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('host-h1-open')), findsOneWidget);
      expect(find.byKey(const Key('session-h1/zeta-open')), findsOneWidget);
      expect(find.byKey(const Key('window-h1/zeta/0:dev-open')), findsOneWidget);
      var prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('tmux-tree-open'), contains('h1/zeta/0:dev'));

      await tester.tap(find.byKey(const Key('tree-collapse')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('host-h1-shut')), findsOneWidget);
      prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('tmux-tree-open'), '[]');
    });

    testWidgets('restored tree-open state survives a restart', (tester) async {
      // Write through the real path (expand-all persists), then
      // unmount and mount fresh: a fresh shell must restore the
      // saved open state. (Mock-value swapping across tests is
      // unreliable here because the prefs instance is cached.)
      final api = _ConsoleApi();
      await _pumpWide(tester, api);
      await tester.tap(find.byKey(const Key('tree-expand')));
      await tester.pumpAndSettle();
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pumpAndSettle();
      await tester.pumpWidget(wrap(HostsScreen(api: api, baseUrl: 'http://127.0.0.1:8787')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('host-h1-open')), findsOneWidget);
      expect(find.byKey(const Key('window-h1/zeta/0:dev-open')), findsOneWidget);
    });
  });

  /// The detail board mounts its own header with some of the same
  /// tooltips/keys, so console-sidebar assertions scope into the
  /// sidebar container.
  Finder inSidebar(Finder finder) => find.descendant(
        of: find.byKey(const Key('console-sidebar')),
        matching: finder,
      );

  group('brand, footer, tooltips (B1/B2/B5, L2)', () {
    testWidgets('brand text, footer count + settings chip, exact tooltips', (tester) async {
      final api = _ConsoleApi();
      await _pumpWide(tester, api);
      expect(find.text('Tmux Chat'), findsOneWidget);
      expect(find.text('本机实时会话'), findsOneWidget);
      expect(find.text('2 台主机 · 3 个 pane'), findsOneWidget);
      expect(find.byKey(const Key('footer-settings')), findsOneWidget);
      expect(inSidebar(find.byTooltip('显示文件面板')), findsOneWidget);
      expect(inSidebar(find.byTooltip('折叠侧边栏')), findsOneWidget);
      expect(inSidebar(find.byTooltip('展开全部')), findsOneWidget);
      expect(inSidebar(find.byTooltip('折叠全部')), findsOneWidget);
      // The grid shell has its own resizer with the same tooltip
      // text; pin the console one via its key.
      expect(
        find.ancestor(
          of: find.byKey(const Key('sidebar-resizer')),
          matching: find.byTooltip('拖动调整侧边栏宽度（双击恢复默认）'),
        ),
        findsOneWidget,
      );
      expect(inSidebar(find.byTooltip('刷新这台主机的 pane 列表')), findsNWidgets(2));
      // Settings moved out of the header row.
      expect(inSidebar(find.byKey(const Key('settings'))), findsNothing);
      // Brand action: management page entry exists.
      expect(inSidebar(find.byKey(const Key('shares-manage'))), findsOneWidget);
    });

    testWidgets('coarse pointers get a swipeable 40px brand row', (tester) async {
      // Reset inline: the framework verifies debug vars before
      // addTearDown callbacks run, so a tearDown reset still fails.
      debugDefaultTargetPlatformOverride = TargetPlatform.android;
      try {
        final api = _ConsoleApi();
        await _pumpWide(tester, api);
        final box = tester.getSize(find.byKey(const Key('grid')));
        expect(box.width, 40);
        expect(box.height, 40);
      } finally {
        debugDefaultTargetPlatformOverride = null;
      }
    });
  });

  group('refresh and starring (A7/A8/D1)', () {
    testWidgets('per-host refresh spins, toasts, and forces a refresh', (tester) async {
      final api = _ConsoleApi();
      await _pumpWide(tester, api);
      // Arm a deferred future for the refresh tap only (the initial
      // load already consumed the canned hosts).
      final deferred = Completer<List<Host>>();
      api.deferredHosts = deferred;
      await tester.tap(find.byKey(const Key('host-refresh-h1')));
      await tester.pump();
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      deferred.complete(_ConsoleApi._consoleHosts);
      await tester.pumpAndSettle();
      expect(api.hostsRefreshCalls.last, isTrue);
      expect(find.text('已刷新'), findsOneWidget);
    });

    testWidgets('header refresh forces a refresh', (tester) async {
      final api = _ConsoleApi();
      await _pumpWide(tester, api);
      api.hostsRefreshCalls.clear();
      await tester.ensureVisible(find.byKey(const Key('header-refresh')));
      await tester.tap(find.byKey(const Key('header-refresh')));
      await tester.pumpAndSettle();
      expect(api.hostsRefreshCalls, [true]);
    });

    testWidgets('starring is silent on success', (tester) async {
      final api = _ConsoleApi();
      await _pumpWide(tester, api);
      await tester.tap(find.byKey(const Key('tree-expand')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('star-h1~%0')));
      await tester.pumpAndSettle();
      expect(find.text('已收藏到主页'), findsNothing);
      expect(find.text('已取消收藏'), findsNothing);
      final button = tester.widget<IconButton>(find.byKey(const Key('star-h1~%0')));
      expect((button.icon as Icon).icon, Icons.star);
    });
  });

  group('recent-block caller wiring (E)', () {
    testWidgets('recent pick touches, clear and rename-favorite are wired', (tester) async {
      final api = _ConsoleApi();
      api.pinsData = {
        'recent': [
          {'pane': 'h1~%0', 'title': 'review', 'number': 7, 'visited_at': 1700000000.0},
          {'pane': 'vanished', 'title': 'gone', 'visited_at': 1700000000.0},
        ],
        'pinned': [
          {'pane': 'h1~%1', 'label': 'shell pin'},
        ],
      };
      api.favoritePanes.add('h1~%1');
      await _pumpWide(tester, api);
      // Out-of-tree unpinned rows are dropped; in-tree rows render.
      // Scoped: the detail board mounts its own RecentBlock too.
      expect(inSidebar(find.byKey(const Key('recent-row-h1~%0'))), findsOneWidget);
      expect(inSidebar(find.byKey(const Key('recent-row-vanished'))), findsNothing);
      expect(inSidebar(find.byKey(const Key('recent-row-h1~%1'))), findsOneWidget);

      // Recent pick selects AND touches (web touches on every selection).
      await tester.tap(inSidebar(find.byKey(const Key('recent-row-h1~%0'))));
      await tester.pumpAndSettle();
      expect(api.touched, contains('h1~%0'));

      // Clear deletes via the api method (FakeApi counts the call).
      await tester.tap(inSidebar(find.byKey(const Key('recent-clear'))));
      await tester.pumpAndSettle();
      expect(api.clearedUnpinnedCalls, 1);

      // Renaming a favorited pin also renames the favorite title.
      await tester.tap(inSidebar(find.byKey(const Key('recent-row-h1~%1'))));
      await tester.pump(const Duration(milliseconds: 50));
      await tester.tap(inSidebar(find.byKey(const Key('recent-row-h1~%1'))));
      await tester.pumpAndSettle();
      final input = inSidebar(find.byKey(const Key('recent-rename-input-h1~%1')));
      expect(input, findsOneWidget);
      await tester.enterText(input, 'renamed pin');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(api.renamedFavorites, contains('h1~%1|renamed pin'));
    });

    testWidgets('file panel hides the recent block and shows the owning host label', (tester) async {
      final api = _ConsoleApi();
      api.pinsData = {
        'recent': [
          {'pane': 'h1~%0', 'title': 'review', 'visited_at': 1700000000.0},
        ],
        'pinned': [],
      };
      await _pumpWide(tester, api);
      expect(inSidebar(find.byKey(const Key('recent-row-h1~%0'))), findsOneWidget);
      await tester.tap(find.byKey(const Key('tree-expand')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('pane-h1~%0')));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.byKey(const Key('sidebar-files-toggle')));
      await tester.tap(find.byKey(const Key('sidebar-files-toggle')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('recent-row-h1~%0')), findsNothing);
      expect(find.byKey(const Key('sidebar-files-host')), findsOneWidget);
      expect(find.text('workstation'), findsWidgets);
    });
  });

  group('collapsed sidebar (C2)', () {
    testWidgets('collapsed shell expands from the detail header, not an overlay', (tester) async {
      final api = _ConsoleApi();
      await _pumpWide(tester, api);
      await tester.ensureVisible(find.byKey(const Key('sidebar-collapse')));
      await tester.tap(find.byKey(const Key('sidebar-collapse')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('sidebar-expand')), findsOneWidget);
      await tester.tap(find.byKey(const Key('sidebar-expand')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('sidebar-collapse')), findsOneWidget);
    });
  });

  group('narrow drawer (C1)', () {
    testWidgets('drawer shut by default with the empty-selection placeholder', (tester) async {
      final api = _ConsoleApi();
      await _pumpNarrow(tester, api);
      final left = tester.getTopLeft(find.byKey(const Key('narrow-drawer'))).dx;
      expect(left, lessThan(0));
      expect(find.text('选择一个会话'), findsOneWidget);
      expect(find.byKey(const Key('drawer-backdrop')), findsNothing);
    });

    testWidgets('menu opens the drawer, selection closes it and shows the detail', (tester) async {
      final api = _ConsoleApi();
      await _pumpNarrow(tester, api);
      await tester.tap(find.byKey(const Key('narrow-menu')));
      await tester.pumpAndSettle();
      expect(tester.getTopLeft(find.byKey(const Key('narrow-drawer'))).dx, 0);
      expect(find.byKey(const Key('drawer-backdrop')), findsOneWidget);
      await tester.tap(find.byKey(const Key('tree-expand')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('pane-h1~%0')));
      await tester.pumpAndSettle();
      expect(tester.getTopLeft(find.byKey(const Key('narrow-drawer'))).dx, lessThan(0));
      expect(find.byKey(const Key('drawer-backdrop')), findsNothing);
      expect(api.touched, contains('h1~%0'));
    });

    testWidgets('backdrop tap and Escape close the drawer', (tester) async {
      final api = _ConsoleApi();
      await _pumpNarrow(tester, api);
      await tester.tap(find.byKey(const Key('narrow-menu')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('drawer-backdrop')));
      await tester.pumpAndSettle();
      expect(tester.getTopLeft(find.byKey(const Key('narrow-drawer'))).dx, lessThan(0));

      await tester.tap(find.byKey(const Key('narrow-menu')));
      await tester.pumpAndSettle();
      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await tester.pumpAndSettle();
      expect(tester.getTopLeft(find.byKey(const Key('narrow-drawer'))).dx, lessThan(0));
    });
  });

  group('empty, error, loading states (L4/L5/L6)', () {
    testWidgets('load failure keeps an empty tree plus a main-area banner, no message', (tester) async {
      final api = _ConsoleApi(hostsError: ApiException(500, 'boom'));
      await _pumpWide(tester, api);
      expect(find.byKey(const Key('console-error-banner')), findsOneWidget);
      expect(find.text('2 台主机 · 3 个 pane'), findsNothing);
      expect(find.text('0 台主机 · 0 个 pane'), findsOneWidget);
      expect(find.text('还没有可用的 tmux pane。'), findsNothing);
    });

    testWidgets('no sidebar spinner while hosts are null', (tester) async {
      final api = _ConsoleApi();
      SharedPreferences.setMockInitialValues({});
      AppTheme.resetForTest();
      addTearDown(AppTheme.resetForTest);
      await tester.pumpWidget(wrap(HostsScreen(api: api, baseUrl: 'http://127.0.0.1:8787')));
      await tester.pump();
      expect(find.byType(CircularProgressIndicator), findsNothing);
      await tester.pumpAndSettle();
    });
  });
}
