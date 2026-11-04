import 'package:desktop_drop/desktop_drop.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/api/api_client.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/theme.dart';
import 'package:tmuxweb_client/widgets/sidebar_file_panel.dart';

import 'screens_test.dart' show FakeApi, wrap;

/// FakeApi whose second upload throws, to prove the first failure
/// stops the batch like web's uploadFiles.
class _FailingSecondUploadApi extends FakeApi {
  @override
  Future<void> uploadFile(String paneRef, String dir, String name, List<int> bytes) async {
    if (name == 'b.txt') throw ApiException(500, 'boom');
    await super.uploadFile(paneRef, dir, name, bytes);
  }
}

/// OS drag-and-drop onto the sidebar file panel (the wide-shell
/// panel was the one surface without it; the main files panel
/// already had it, covered by drop_pinch_test.dart).
///
/// A real OS drop cannot be simulated in flutter test, so the tests
/// drive the [DropTarget] callbacks directly — the wiring the panel
/// owns — with [DropItemFile.fromData] items, whose bytes are in
/// memory (no real file IO in the test zone).
void main() {
  final pane = Pane(ref: '%7', title: 'review', command: 'codex', session: 'codex', path: '/srv/work');

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    AppTheme.resetForTest();
  });

  tearDown(() {
    AppTheme.resetForTest();
  });

  /// The test host platform is not a desktop target, so the
  /// desktop gate is opened explicitly per test. The override must
  /// be cleared at the end of the test body itself: the framework
  /// verifies foundation debug variables before tearDowns run, so
  /// an addTearDown reset lands too late.
  Future<void> pumpDesktopPanel(WidgetTester tester, FakeApi api) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.linux;
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
          pane: pane,
          initialPath: '/srv/work',
          onClose: () {},
          onDirectoryChanged: (_) {},
        ),
      ),
    )));
    await tester.pumpAndSettle();
  }

  void clearPlatformOverride() {
    debugDefaultTargetPlatformOverride = null;
  }

  DropTarget dropTarget(WidgetTester tester) =>
      tester.widget<DropTarget>(find.byKey(const Key('sidebar-files-drop-target')));

  testWidgets('desktop sidebar panel has a drop target, no overlay at rest', (tester) async {
    await pumpDesktopPanel(tester, FakeApi());

    expect(find.byKey(const Key('sidebar-files-drop-target')), findsOneWidget);
    expect(find.byKey(const Key('sidebar-files-drop-overlay')), findsNothing);
    clearPlatformOverride();
  });

  testWidgets('drag hover shows the drop overlay until the drag leaves', (tester) async {
    await pumpDesktopPanel(tester, FakeApi());

    dropTarget(tester).onDragEntered!(
      DropEventDetails(localPosition: Offset.zero, globalPosition: const Offset(140, 300)),
    );
    await tester.pump();
    expect(find.byKey(const Key('sidebar-files-drop-overlay')), findsOneWidget);
    expect(find.text('松开以上传到当前目录'), findsOneWidget);

    dropTarget(tester).onDragExited!(
      DropEventDetails(localPosition: Offset.zero, globalPosition: const Offset(140, 300)),
    );
    await tester.pump();
    expect(find.byKey(const Key('sidebar-files-drop-overlay')), findsNothing);
    clearPlatformOverride();
  });

  testWidgets('drop uploads through the existing upload path', (tester) async {
    final api = FakeApi();
    await pumpDesktopPanel(tester, api);

    dropTarget(tester).onDragDone!(
      DropDoneDetails(
        files: [DropItemFile.fromData(Uint8List.fromList([104, 105]), path: 'dropped.txt')],
        localPosition: const Offset(140, 300),
        globalPosition: const Offset(140, 300),
      ),
    );
    await tester.pumpAndSettle();

    expect(api.uploads, ['/srv/work/dropped.txt|2']);
    // Web's uploadFiles always reports the count, even for one file.
    expect(find.text('已上传 1 个文件'), findsOneWidget);
    expect(find.byKey(const Key('sidebar-files-drop-overlay')), findsNothing);
    clearPlatformOverride();
  });

  testWidgets('a failed upload stops the drop like web, with <name>: <error>', (tester) async {
    final api = _FailingSecondUploadApi();
    await pumpDesktopPanel(tester, api);

    dropTarget(tester).onDragDone!(
      DropDoneDetails(
        files: [
          DropItemFile.fromData(Uint8List.fromList([104, 105]), path: 'a.txt'),
          DropItemFile.fromData(Uint8List.fromList([106]), path: 'b.txt'),
        ],
        localPosition: const Offset(140, 300),
        globalPosition: const Offset(140, 300),
      ),
    );
    await tester.pumpAndSettle();

    // Only the first file went up; no success message follows.
    expect(api.uploads, ['/srv/work/a.txt|2']);
    expect(find.text('b.txt: boom'), findsOneWidget);
    expect(find.textContaining('已上传'), findsNothing);
    clearPlatformOverride();
  });

  testWidgets('mobile sidebar panel has no drop target, picker button stays', (tester) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(wrap(Scaffold(
      body: SizedBox(
        width: 280,
        height: 600,
        child: SidebarFilePanel(
          api: FakeApi(),
          pane: pane,
          initialPath: '/srv/work',
          onClose: () {},
          onDirectoryChanged: (_) {},
        ),
      ),
    )));
    await tester.pumpAndSettle();

    expect(find.byType(DropTarget), findsNothing);
    expect(find.byKey(const Key('sidebar-files-upload')), findsOneWidget);
    clearPlatformOverride();
  });
}
