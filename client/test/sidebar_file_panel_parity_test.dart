import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/api/api_client.dart';
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

  testWidgets('F2: loaded listing shows web count status, no list tail', (tester) async {
    await pumpPanel(tester, FakeApi());
    expect(find.text('2 项 · 拖拽文件可上传'), findsOneWidget);
    expect(find.text('条目太多，只显示了一部分'), findsNothing);
  });

  testWidgets('F2: truncated listing shows the （已截断） status in the footer', (tester) async {
    await pumpPanel(tester, _TruncatedApi());
    expect(find.text('2 项（已截断） · 拖拽文件可上传'), findsOneWidget);
    expect(find.text('条目太多，只显示了一部分'), findsNothing);
  });

  testWidgets('F2: failed load shows the error in the footer, keeping retry', (tester) async {
    await pumpPanel(tester, _FailingListApi());
    // The error page body and the footer both show it; assert on the
    // footer widget itself.
    final status = tester.widget<Text>(find.byKey(const Key('sidebar-files-status')));
    expect(status.data, 'boom');
    expect(find.text('重试'), findsOneWidget);
  });
}

/// Listing with truncated set, for the F2 footer text.
class _TruncatedApi extends FakeApi {
  @override
  Future<FileListing> listFiles(String paneRef, {String path = '', bool hidden = false}) async =>
      FileListing(
        path: path.isEmpty ? '/srv/work' : path,
        parent: path.isEmpty ? null : '/srv',
        entries: [
          FileEntry(name: 'notes.md', path: '/srv/work/notes.md', isDir: false, size: 2048),
          FileEntry(name: 'sub', path: '/srv/work/sub', isDir: true),
        ],
        truncated: true,
      );
}

/// listFiles always throws, for the F2 error status.
class _FailingListApi extends FakeApi {
  @override
  Future<FileListing> listFiles(String paneRef, {String path = '', bool hidden = false}) async {
    throw ApiException(500, 'boom');
  }
}
