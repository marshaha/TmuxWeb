import 'package:flutter/gestures.dart';
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
  final pane = Pane(
      ref: '%7',
      title: 'review',
      command: 'codex',
      session: 'codex',
      path: '/srv/work');
  final remotePane = Pane(
      ref: 'abc123~%7',
      title: 'remote',
      command: 'codex',
      session: 'codex',
      path: '/srv/work');

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    AppTheme.resetForTest();
    const channel = MethodChannel('plugins.flutter.io/path_provider');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(
      channel,
      (call) async => '/tmp',
    );
    addTearDown(() => TestDefaultBinaryMessengerBinding
        .instance.defaultBinaryMessenger
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

  testWidgets('F1: explicit hostLabel shows as the footer host chip',
      (tester) async {
    await pumpPanel(tester, FakeApi(), hostLabel: 'my-server');
    expect(find.byKey(const Key('sidebar-files-host')), findsOneWidget);
    expect(find.text('my-server'), findsOneWidget);
  });

  testWidgets('F1: no hostLabel falls back to remote for a ~ pane ref',
      (tester) async {
    await pumpPanel(tester, FakeApi(), forPane: remotePane);
    expect(find.text('remote'), findsOneWidget);
  });

  testWidgets('F1: no hostLabel falls back to localhost for a local pane ref',
      (tester) async {
    await pumpPanel(tester, FakeApi());
    expect(find.text('localhost'), findsOneWidget);
  });

  testWidgets('F2: loaded listing shows web count status, no list tail',
      (tester) async {
    await pumpPanel(tester, FakeApi());
    expect(find.text('2 项 · 拖拽文件可上传'), findsOneWidget);
    expect(find.text('条目太多，只显示了一部分'), findsNothing);
  });

  testWidgets('F2: truncated listing shows the （已截断） status in the footer',
      (tester) async {
    await pumpPanel(tester, _TruncatedApi());
    expect(find.text('2 项（已截断） · 拖拽文件可上传'), findsOneWidget);
    expect(find.text('条目太多，只显示了一部分'), findsNothing);
  });

  testWidgets('F3: parent row is first and navigates up, bar up button kept',
      (tester) async {
    final seen = <String>[];
    await pumpPanel(tester, FakeApi(), seen: seen);
    final parentRow = find.byKey(const Key('sidebar-file-parent'));
    expect(parentRow, findsOneWidget);
    expect(find.text('..'), findsOneWidget);
    // The parent row precedes the first entry row.
    final parentOffset = tester.getTopLeft(parentRow);
    final firstEntryOffset =
        tester.getTopLeft(find.byKey(const Key('sidebar-file-notes.md')));
    expect(parentOffset.dy, lessThan(firstEntryOffset.dy));
    await tester.tap(parentRow);
    await tester.pumpAndSettle();
    expect(seen.last, '/srv');
    // Web's file bar has no up control, but Flutter's stays: removing
    // navigation would be a regression, so both exist.
    expect(find.byKey(const Key('sidebar-files-up')), findsOneWidget);
  });

  testWidgets('F6: F2 renames the single checkbox pick', (tester) async {
    await pumpPanel(tester, FakeApi());
    await tester.tap(find.byKey(const Key('sidebar-file-check-notes.md')));
    await tester.sendKeyEvent(LogicalKeyboardKey.f2);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-file-rename-input')), findsOneWidget);
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('sidebar-file-rename-input')))
          .controller!
          .text,
      'notes.md',
    );
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
  });

  testWidgets('F6: F2 with two picks renames the focused row (web rule)',
      (tester) async {
    await pumpPanel(tester, FakeApi());
    await tester.tap(find.byKey(const Key('sidebar-file-check-notes.md')));
    await tester.tap(find.byKey(const Key('sidebar-file-check-sub')));
    await tester.sendKeyEvent(LogicalKeyboardKey.f2);
    await tester.pumpAndSettle();
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('sidebar-file-rename-input')))
          .controller!
          .text,
      'sub',
    );
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
  });

  testWidgets('F6: Delete deletes the focused row when nothing is picked',
      (tester) async {
    await pumpPanel(tester, FakeApi());
    // Check then uncheck: selection empties but the row stays focused,
    // like web's checkbox click setting the keydown row.
    await tester.tap(find.byKey(const Key('sidebar-file-check-notes.md')));
    await tester.tap(find.byKey(const Key('sidebar-file-check-notes.md')));
    await tester.sendKeyEvent(LogicalKeyboardKey.delete);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-file-delete-ok')), findsOneWidget);
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
  });

  testWidgets('F6: shortcuts do nothing while the path field has focus',
      (tester) async {
    await pumpPanel(tester, FakeApi());
    await tester.tap(find.byKey(const Key('sidebar-files-path')));
    await tester.sendKeyEvent(LogicalKeyboardKey.f2);
    await tester.pump();
    expect(find.byKey(const Key('sidebar-file-rename-input')), findsNothing);
    await tester.sendKeyEvent(LogicalKeyboardKey.delete);
    await tester.pump();
    expect(find.byKey(const Key('sidebar-file-delete-ok')), findsNothing);
  });

  testWidgets('F4: right-click file row shows menu without 打开; 下载 saves', (tester) async {
    await pumpPanel(tester, FakeApi());
    await tester.tap(find.byKey(const Key('sidebar-file-notes.md')), buttons: kSecondaryButton);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-file-menu-download')), findsOneWidget);
    expect(find.byKey(const Key('sidebar-file-menu-open')), findsNothing);
    await tester.tap(find.byKey(const Key('sidebar-file-menu-download')));
    await tester.pumpAndSettle();
    expect(find.textContaining('已保存到'), findsOneWidget);
  });

  testWidgets('F4: right-click dir row shows 打开 and 打包下载; 打开 navigates', (tester) async {
    final seen = <String>[];
    await pumpPanel(tester, FakeApi(), seen: seen);
    await tester.tap(find.byKey(const Key('sidebar-file-sub')), buttons: kSecondaryButton);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-file-menu-open')), findsOneWidget);
    expect(find.byKey(const Key('sidebar-file-menu-zipdir')), findsOneWidget);
    await tester.tap(find.byKey(const Key('sidebar-file-menu-open')));
    await tester.pumpAndSettle();
    expect(seen.last, '/srv/work/sub');
  });

  testWidgets('F4: long-press opens the same menu; 重命名 opens the rename dialog', (tester) async {
    await pumpPanel(tester, FakeApi());
    await tester.longPress(find.byKey(const Key('sidebar-file-notes.md')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-file-menu-rename')), findsOneWidget);
    await tester.tap(find.byKey(const Key('sidebar-file-menu-rename')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-file-rename-input')), findsOneWidget);
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
  });

  testWidgets('F4: Escape dismisses the menu; parent row has no menu', (tester) async {
    await pumpPanel(tester, FakeApi());
    await tester.tap(find.byKey(const Key('sidebar-file-notes.md')), buttons: kSecondaryButton);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-file-menu-rename')), findsOneWidget);
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-file-menu-rename')), findsNothing);
    await tester.tap(find.byKey(const Key('sidebar-file-parent')), buttons: kSecondaryButton);
    await tester.pump();
    expect(find.byKey(const Key('sidebar-file-menu-rename')), findsNothing);
  });

  testWidgets('F2: failed load shows the error in the footer, keeping retry',
      (tester) async {
    await pumpPanel(tester, _FailingListApi());
    // The error page body and the footer both show it; assert on the
    // footer widget itself.
    final status =
        tester.widget<Text>(find.byKey(const Key('sidebar-files-status')));
    expect(status.data, 'boom');
    expect(find.text('重试'), findsOneWidget);
  });

  testWidgets('F5: Shift-click range-selects from the checkbox anchor', (tester) async {
    final seen = <String>[];
    await pumpPanel(tester, FakeApi(), seen: seen);
    await tester.tap(find.byKey(const Key('sidebar-file-check-notes.md')));
    await tester.pump();
    await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
    await tester.tap(find.byKey(const Key('sidebar-file-sub')));
    await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
    await tester.pumpAndSettle();
    expect(tester.widget<Checkbox>(find.byKey(const Key('sidebar-file-check-notes.md'))).value, isTrue);
    expect(tester.widget<Checkbox>(find.byKey(const Key('sidebar-file-check-sub'))).value, isTrue);
    // Range-select never navigates or downloads.
    expect(seen, ['/srv/work']);
  });

  testWidgets('F5: Ctrl-click toggles a pick on and off', (tester) async {
    await pumpPanel(tester, FakeApi());
    await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
    await tester.tap(find.byKey(const Key('sidebar-file-sub')));
    await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
    await tester.pump();
    expect(tester.widget<Checkbox>(find.byKey(const Key('sidebar-file-check-sub'))).value, isTrue);
    await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
    await tester.tap(find.byKey(const Key('sidebar-file-sub')));
    await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
    await tester.pump();
    expect(tester.widget<Checkbox>(find.byKey(const Key('sidebar-file-check-sub'))).value, isFalse);
  });

  testWidgets('F5: Shift-click with no anchor yet falls back to a toggle', (tester) async {
    await pumpPanel(tester, FakeApi());
    await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
    await tester.tap(find.byKey(const Key('sidebar-file-notes.md')));
    await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
    await tester.pump();
    expect(tester.widget<Checkbox>(find.byKey(const Key('sidebar-file-check-notes.md'))).value, isTrue);
    expect(tester.widget<Checkbox>(find.byKey(const Key('sidebar-file-check-sub'))).value, isFalse);
  });

  testWidgets('F5: plain click still downloads a file and keeps checkboxes working', (tester) async {
    await pumpPanel(tester, FakeApi());
    await tester.tap(find.byKey(const Key('sidebar-file-notes.md')));
    await tester.pumpAndSettle();
    expect(find.textContaining('已保存到'), findsOneWidget);
  });

}

/// Listing with truncated set, for the F2 footer text.
class _TruncatedApi extends FakeApi {
  @override
  Future<FileListing> listFiles(String paneRef,
          {String path = '', bool hidden = false}) async =>
      FileListing(
        path: path.isEmpty ? '/srv/work' : path,
        parent: path.isEmpty ? null : '/srv',
        entries: [
          FileEntry(
              name: 'notes.md',
              path: '/srv/work/notes.md',
              isDir: false,
              size: 2048),
          FileEntry(name: 'sub', path: '/srv/work/sub', isDir: true),
        ],
        truncated: true,
      );
}

/// listFiles always throws, for the F2 error status.
class _FailingListApi extends FakeApi {
  @override
  Future<FileListing> listFiles(String paneRef,
      {String path = '', bool hidden = false}) async {
    throw ApiException(500, 'boom');
  }
}
