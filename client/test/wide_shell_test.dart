import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart';
import 'package:tmuxweb_client/theme.dart';

import 'screens_test.dart' show FakeApi, wrap;

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    AppTheme.resetForTest();
  });

  Future<void> pumpWide(WidgetTester tester) async {
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://test.local')));
    await tester.pumpAndSettle();
  }

  testWidgets('wide shows the sidebar tree and the board detail together', (tester) async {
    await pumpWide(tester);
    // Sidebar tree (pane tile) and detail board header are onstage at once:
    // the tree is not a full-screen page anymore.
    // The tree starts collapsed (web parity A2) — expand to see tiles.
    await tester.tap(find.byKey(const Key('tree-expand')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('pane-%7')), findsOneWidget);
    expect(find.text('主页板'), findsOneWidget);
    expect(find.byKey(const Key('chat-composer')), findsNothing);
  });

  testWidgets('wide tapping a pane selects it into the detail pane, no push', (tester) async {
    await pumpWide(tester);
    await tester.tap(find.byKey(const Key('tree-expand')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('pane-%7')));
    await tester.pumpAndSettle();
    // Embedded chat composer appears while the sidebar tree stays onstage.
    expect(find.byKey(const Key('chat-composer')), findsOneWidget);
    expect(find.byKey(const Key('pane-%7')), findsOneWidget);
    expect(find.byKey(const Key('shell-detail-close')), findsOneWidget);
    expect(find.text('来自 %7 的回复'), findsOneWidget);
  });

  testWidgets('wide detail header closes back to the board', (tester) async {
    await pumpWide(tester);
    await tester.tap(find.byKey(const Key('tree-expand')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('pane-%7')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('shell-detail-close')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('chat-composer')), findsNothing);
    expect(find.text('主页板'), findsOneWidget);
  });

  testWidgets('wide detail header opens the full chat page', (tester) async {
    await pumpWide(tester);
    await tester.tap(find.byKey(const Key('tree-expand')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('pane-%7')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('shell-detail-open')));
    await tester.pumpAndSettle();
    // The pushed full chat covers the shell: the detail header is offstage.
    expect(find.byKey(const Key('shell-detail-close')), findsNothing);
    expect(find.byKey(const Key('chat-composer')), findsOneWidget);
  });

  testWidgets('narrow pane tap selects into the inline detail and closes the drawer', (tester) async {
    // C1 replaced the old narrow push-a-chat-page flow: the console is
    // detail-first with a slide-over drawer, and selecting a pane
    // closes the drawer while the chat shows inline in the detail.
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://test.local')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('narrow-menu')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('tree-expand')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('pane-%7')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('chat-composer')), findsOneWidget);
    // The drawer slid shut (off-screen), but it stays built — the
    // tile is still in the tree, just off-screen, not on a pushed route.
    expect(tester.getTopLeft(find.byKey(const Key('narrow-drawer'))).dx, lessThan(0));
    expect(find.byKey(const Key('pane-%7')), findsOneWidget);
  });
}
