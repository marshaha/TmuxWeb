import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart';
import 'package:tmuxweb_client/screens/settings_screen.dart';
import 'package:tmuxweb_client/theme.dart';
import 'package:tmuxweb_client/widgets/sidebar_file_panel.dart';

import 'screens_test.dart' show FakeApi, wrap;

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    AppTheme.resetForTest();
  });

  tearDown(() {
    AppTheme.resetForTest();
  });

  Future<void> pumpWide(WidgetTester tester, {String? initialPaneRef, Map<String, Object>? initialValues}) async {
    if (initialValues != null) SharedPreferences.setMockInitialValues(initialValues);
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(
        wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://test.local', initialPaneRef: initialPaneRef)));
    await tester.pumpAndSettle();
  }

  test('sidebar view round-trips per pane through SidebarViewMemory, capped at 60', () async {
    await SidebarViewMemory.save('%7', const SidebarFileView(open: true, path: '/srv/work').encode());
    await SidebarViewMemory.save('abc~%0', const SidebarFileView(open: false).encode());
    var views = await SidebarViewMemory.load();
    expect(SidebarFileView.tryParse(views['%7'])?.open, isTrue);
    expect(SidebarFileView.tryParse(views['%7'])?.path, '/srv/work');
    expect(SidebarFileView.tryParse(views['abc~%0'])?.open, isFalse);
    // Re-saving an existing pane moves it to most-recent, so filling
    // 60 more panes trims the two oldest (%7, abc~%0) first.
    for (var i = 0; i < 60; i++) {
      await SidebarViewMemory.save('pane-$i', const SidebarFileView(open: true, path: '/d').encode());
    }
    views = await SidebarViewMemory.load();
    expect(views.length, 60);
    expect(views.containsKey('%7'), isFalse);
    expect(views.containsKey('abc~%0'), isFalse);
    expect(views.containsKey('pane-59'), isTrue);
    // Legacy plain scaffolding values still parse; junk does not.
    expect(SidebarFileView.tryParse('files:/old/dir')?.path, '/old/dir');
    expect(SidebarFileView.tryParse('files:/old/dir')?.open, isTrue);
    expect(SidebarFileView.tryParse('not-a-view'), isNull);
    expect(SidebarFileView.tryParse(null), isNull);
  });

  testWidgets('wide memory-on restores the open panel per pane, unrecorded panes show the tree', (tester) async {
    await pumpWide(tester, initialPaneRef: '%7', initialValues: {
      'tmux-sidebar-memory': 'pane',
      'tmux-files-view': jsonEncode({
        '%7': const SidebarFileView(open: true, path: '/srv/work').encode(),
        'abc~%0': const SidebarFileView(open: false).encode(),
      }),
    });
    // %7 was recorded open: the panel, not the tree, is in the sidebar.
    expect(find.byKey(const Key('sidebar-files-close')), findsOneWidget);
    expect(tester.widget<TextField>(find.byKey(const Key('sidebar-files-path'))).controller!.text, '/srv/work');
    expect(find.text('notes.md'), findsOneWidget);
    // Switching to the recorded-closed pane shows the tree again.
    await tester.pumpWidget(
        wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://test.local', initialPaneRef: 'abc~%0')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-files-close')), findsNothing);
    // The tree starts collapsed (web parity A2) — expand to see tiles.
    await tester.tap(find.byKey(const Key('tree-expand')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('pane-abc~%0')), findsOneWidget);
    // Switching back restores %7's panel at its directory.
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://test.local', initialPaneRef: '%7')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-files-close')), findsOneWidget);
    expect(find.text('notes.md'), findsOneWidget);
  });

  testWidgets('wide memory-off starts on the tree and the toggle opens the pane directory without writing',
      (tester) async {
    final stored = jsonEncode({
      '%7': const SidebarFileView(open: true, path: '/srv/work').encode(),
    });
    await pumpWide(tester, initialPaneRef: '%7', initialValues: {
      'tmux-sidebar-memory': 'none',
      'tmux-files-view': stored,
    });
    // A fresh load always starts on the session tree, even with a
    // recorded open panel for this pane.
    expect(find.byKey(const Key('sidebar-files-close')), findsNothing);
    // The tree starts collapsed (web parity A2) — expand to see tiles.
    await tester.tap(find.byKey(const Key('tree-expand')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('pane-%7')), findsOneWidget);
    await tester.ensureVisible(find.byKey(const Key('sidebar-files-toggle')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('sidebar-files-toggle')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-files-close')), findsOneWidget);
    expect(tester.widget<TextField>(find.byKey(const Key('sidebar-files-path'))).controller!.text, '/srv/work');
    // Browsing inside the panel with memory off writes nothing.
    await tester.tap(find.byKey(const Key('sidebar-file-sub')));
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(find.byKey(const Key('sidebar-files-path'))).controller!.text, '/srv/work/sub');
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString('tmux-files-view'), stored);
  });

  testWidgets('narrow initialPaneRef auto-opens the chat post-frame', (tester) async {
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://test.local', initialPaneRef: '%7')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('chat-composer')), findsOneWidget);
    expect(find.text('来自 %7 的回复'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('rapid repeated deep-link values push at most one chat and do not crash', (tester) async {
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://test.local', initialPaneRef: '%7')));
    // Rebuild with different values before settling: the first load is
    // still in flight and a push may already be scheduled.
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://test.local', initialPaneRef: 'abc~%0')));
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://test.local', initialPaneRef: '%7')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('chat-composer')), findsOneWidget);
    expect(tester.takeException(), isNull);
    // Rebuilding with the same value again must not stack a second chat.
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://test.local', initialPaneRef: '%7')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('chat-composer')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('wide initialPaneRef still shows the detail pane, no pushed chat', (tester) async {
    await pumpWide(tester, initialPaneRef: '%7');
    expect(find.byKey(const Key('shell-detail-close')), findsOneWidget);
    // The tree starts collapsed (web parity A2) — expand to see tiles.
    await tester.tap(find.byKey(const Key('tree-expand')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('pane-%7')), findsOneWidget);
    expect(find.byKey(const Key('chat-composer')), findsOneWidget);
  });
}
