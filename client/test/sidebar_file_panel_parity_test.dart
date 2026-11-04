import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/theme.dart';
import 'package:tmuxweb_client/widgets/sidebar_file_panel.dart';

import 'screens_test.dart' show FakeApi, wrap;

/// Parity tests for the sidebar file panel gaps (F1–F7) against web's
/// #file-panel (web/index.html). Mirrors the pump harness in
/// sidebar_panel_actions_test.dart: wide surface, path_provider
/// pointed at /tmp so row saves succeed, no async IO before the
/// first pumpWidget.
void main() {
  final pane = Pane(ref: '%7', title: 'review', command: 'codex', session: 'codex', path: '/srv/work');
  final remotePane =
      Pane(ref: 'abc123~%7', title: 'remote', command: 'codex', session: 'codex', path: '/srv/work');

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    AppTheme.resetForTest();
    const channel = MethodChannel('plugins.flutter.io/path_provider');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      channel,
      (call) async => '/tmp',
    );
    addTearDown(() => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null));
  });

  tearDown(() {
    AppTheme.resetForTest();
  });

  Future<void> pumpPanel(
    WidgetTester tester,
    FakeApi api, {
    Pane? forPane,
    String? hostLabel,
    Future<List<({String name, List<int> bytes})>> Function()? pickUploads,
    List<String>? seen,
  }) async {
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(wrap(Scaffold(
      body: SizedBox(
        width: 280,
        height: 600,
        child: SidebarFilePanel(
          api: api,
          pane: forPane ?? pane,
          initialPath: '/srv/work',
          onClose: () {},
          onDirectoryChanged: (path) => seen?.add(path),
          pickUploads: pickUploads,
          hostLabel: hostLabel,
        ),
      ),
    )));
    await tester.pumpAndSettle();
  }

  testWidgets('F1: explicit hostLabel shows as the footer host chip', (tester) async {
    await pumpPanel(tester, FakeApi(), hostLabel: 'my-server');
    expect(find.byKey(const Key('sidebar-files-host')), findsOneWidget);
    expect(find.text('my-server'), findsOneWidget);
  });

  testWidgets('F1: no hostLabel falls back to remote for a ~ pane ref', (tester) async {
    await pumpPanel(tester, FakeApi(), forPane: remotePane);
    expect(find.text('remote'), findsOneWidget);
  });

  testWidgets('F1: no hostLabel falls back to localhost for a local pane ref', (tester) async {
    await pumpPanel(tester, FakeApi());
    expect(find.text('localhost'), findsOneWidget);
  });
}
