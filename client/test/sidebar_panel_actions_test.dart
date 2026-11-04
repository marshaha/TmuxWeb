import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/api/api_client.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/theme.dart';
import 'package:tmuxweb_client/widgets/sidebar_file_panel.dart';

import 'screens_test.dart' show FakeApi, wrap;

/// FakeApi with a listFiles counter, so a refresh after rename/delete/
/// upload is observable even though the fake listing never changes.
class CountingApi extends FakeApi {
  int lists = 0;

  @override
  Future<FileListing> listFiles(String paneRef, {String path = '', bool hidden = false}) async {
    lists++;
    return super.listFiles(paneRef, path: path, hidden: hidden);
  }
}

/// FakeApi whose second upload throws, to prove the first failure
/// stops the batch like web's uploadFiles.
class FailingUploadApi extends FakeApi {
  @override
  Future<void> uploadFile(String paneRef, String dir, String name, List<int> bytes) async {
    if (name == 'b.txt') throw ApiException(500, 'boom');
    await super.uploadFile(paneRef, dir, name, bytes);
  }
}

void main() {
  final pane = Pane(ref: '%7', title: 'review', command: 'codex', session: 'codex', path: '/srv/work');

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    AppTheme.resetForTest();
    // saveFileBytes writes under getApplicationDocumentsDirectory;
    // point that channel at /tmp so zip/row saves succeed in tests
    // (same mock as files_screen_test.dart).
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
    Future<List<({String name, List<int> bytes})>> Function()? pickUploads,
    List<String>? seen,
  }) async {
    // The panel is wide-shell only (280px sidebar), so mount it at
    // the wide surface even though it is not inside HostsScreen here.
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
          onDirectoryChanged: (path) => seen?.add(path),
          pickUploads: pickUploads,
        ),
      ),
    )));
    await tester.pumpAndSettle();
  }

  testWidgets('panel still loads at initialPath and reports the resolved directory', (tester) async {
    final seen = <String>[];
    await pumpPanel(tester, FakeApi(), seen: seen);
    expect(tester.widget<TextField>(find.byKey(const Key('sidebar-files-path'))).controller!.text, '/srv/work');
    expect(seen, ['/srv/work']);
    expect(find.text('notes.md'), findsOneWidget);
    expect(find.text('sub'), findsOneWidget);
  });

  testWidgets('upload via picker goes through the fake API and reports the count', (tester) async {
    final api = CountingApi();
    await pumpPanel(
      tester,
      api,
      pickUploads: () async => [(name: 'a.txt', bytes: [1, 2]), (name: 'b.txt', bytes: [3])],
    );
    final listsBefore = api.lists;
    await tester.tap(find.byKey(const Key('sidebar-files-upload')));
    await tester.pumpAndSettle();
    expect(api.uploads, ['/srv/work/a.txt|2', '/srv/work/b.txt|1']);
    expect(find.text('已上传 2 个文件'), findsOneWidget);
    // Success refreshes the listing.
    expect(api.lists, greaterThan(listsBefore));
  });

  testWidgets('upload stops on the first failure with "<name>: <error>"', (tester) async {
    final api = FailingUploadApi();
    await pumpPanel(
      tester,
      api,
      pickUploads: () async => [(name: 'a.txt', bytes: [1]), (name: 'b.txt', bytes: [2]), (name: 'c.txt', bytes: [3])],
    );
    await tester.tap(find.byKey(const Key('sidebar-files-upload')));
    await tester.pumpAndSettle();
    expect(api.uploads, ['/srv/work/a.txt|1']);
    expect(find.textContaining('b.txt:'), findsOneWidget);
  });

  testWidgets('zip with nothing selected zips the current directory', (tester) async {
    await pumpPanel(tester, FakeApi());
    expect(tester.widget<IconButton>(find.byKey(const Key('sidebar-files-zip'))).tooltip, '打包下载当前目录');
    await tester.tap(find.byKey(const Key('sidebar-files-zip')));
    await tester.pumpAndSettle();
    // Fake zip bytes saved under /tmp/tmuxweb/work.zip (dir stem).
    expect(find.textContaining('已保存到'), findsOneWidget);
    expect(find.textContaining('work.zip'), findsOneWidget);
  });

  testWidgets('zip with a selection zips the picked rows and updates the bar tooltip', (tester) async {
    await pumpPanel(tester, FakeApi());
    await tester.tap(find.byKey(const Key('sidebar-file-check-notes.md')));
    await tester.pumpAndSettle();
    expect(tester.widget<IconButton>(find.byKey(const Key('sidebar-files-zip'))).tooltip, '打包下载选中的 1 项');
    expect(find.text('下载 zip (1)'), findsOneWidget);
    await tester.tap(find.byKey(const Key('sidebar-files-footer-zip')));
    await tester.pumpAndSettle();
    expect(find.textContaining('已保存到'), findsOneWidget);
  });

  testWidgets('rename is offered for exactly 1 selection and hidden otherwise', (tester) async {
    final api = CountingApi();
    await pumpPanel(tester, api);
    // No selection: no footer at all.
    expect(find.byKey(const Key('sidebar-files-footer-rename')), findsNothing);
    await tester.tap(find.byKey(const Key('sidebar-file-check-notes.md')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-files-footer-rename')), findsOneWidget);
    await tester.tap(find.byKey(const Key('sidebar-files-footer-rename')));
    await tester.pumpAndSettle();
    final field = find.byKey(const Key('sidebar-file-rename-input'));
    expect(field, findsOneWidget);
    expect(tester.widget<TextField>(field).controller!.text, 'notes.md');
    final listsBefore = api.lists;
    await tester.enterText(field, 'readme.md');
    await tester.tap(find.byKey(const Key('sidebar-file-rename-ok')));
    await tester.pumpAndSettle();
    expect(api.renames, ['/srv/work/notes.md -> readme.md']);
    expect(api.lists, greaterThan(listsBefore));
    // Refresh cleared the selection (web's openFiles clears picked),
    // so re-pick one row, then select all: with two picked, rename
    // disappears (web hides it unless exactly 1).
    await tester.tap(find.byKey(const Key('sidebar-file-check-notes.md')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('sidebar-files-select-all')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-files-footer-rename')), findsNothing);
  });

  testWidgets('delete asks first, cancels cleanly, and refreshes on confirm', (tester) async {
    final api = CountingApi();
    await pumpPanel(tester, api);
    await tester.tap(find.byKey(const Key('sidebar-file-check-sub')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('sidebar-files-footer-delete')));
    await tester.pumpAndSettle();
    // Directory rows get the recursive checkbox, defaulting to on.
    expect(find.byKey(const Key('sidebar-file-delete-recursive')), findsOneWidget);
    expect(find.text('sub'), findsWidgets);
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
    expect(api.deletes, isEmpty);
    final listsBefore = api.lists;
    await tester.tap(find.byKey(const Key('sidebar-files-footer-delete')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('sidebar-file-delete-ok')));
    await tester.pumpAndSettle();
    expect(api.deletes, ['/srv/work/sub|recursive=true']);
    expect(api.lists, greaterThan(listsBefore));
  });

  testWidgets('select-all toggles to cancel-all, clear hides when all picked', (tester) async {
    await pumpPanel(tester, FakeApi());
    expect(find.byKey(const Key('sidebar-files-select-all')), findsNothing);
    await tester.tap(find.byKey(const Key('sidebar-file-check-notes.md')));
    await tester.pumpAndSettle();
    expect(find.text('全选'), findsOneWidget);
    expect(find.byKey(const Key('sidebar-files-select-none')), findsOneWidget);
    // Checking a row must not navigate away.
    expect(tester.widget<TextField>(find.byKey(const Key('sidebar-files-path'))).controller!.text, '/srv/work');
    await tester.tap(find.byKey(const Key('sidebar-files-select-all')));
    await tester.pumpAndSettle();
    expect(find.text('取消全选'), findsOneWidget);
    // Web hides clear when all are picked (select-all already undoes).
    expect(find.byKey(const Key('sidebar-files-select-none')), findsNothing);
    expect(tester.widget<Checkbox>(find.byKey(const Key('sidebar-file-check-notes.md'))).value, isTrue);
    expect(tester.widget<Checkbox>(find.byKey(const Key('sidebar-file-check-sub'))).value, isTrue);
    // Tapping 取消全选 clears the footer entirely.
    await tester.tap(find.byKey(const Key('sidebar-files-select-all')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-files-select-all')), findsNothing);
    expect(find.byKey(const Key('sidebar-files-footer-delete')), findsNothing);
    // Clear empties a partial selection too.
    await tester.tap(find.byKey(const Key('sidebar-file-check-notes.md')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('sidebar-files-select-none')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sidebar-files-footer-zip')), findsNothing);
  });

  testWidgets('row tap still navigates dirs and checkbox taps do not', (tester) async {
    await pumpPanel(tester, FakeApi());
    await tester.tap(find.byKey(const Key('sidebar-file-sub')));
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(find.byKey(const Key('sidebar-files-path'))).controller!.text, '/srv/work/sub');
  });

  group('editable path input (web #file-path)', () {
    Finder pathField() => find.byKey(const Key('sidebar-files-path'));
    String pathText(WidgetTester tester) => tester.widget<TextField>(pathField()).controller!.text;

    testWidgets('Enter commits the typed path and the field follows the listing', (tester) async {
      final seen = <String>[];
      await pumpPanel(tester, FakeApi(), seen: seen);
      await tester.enterText(pathField(), '/srv/work/sub');
      await tester.testTextInput.receiveAction(TextInputAction.go);
      await tester.pumpAndSettle();
      expect(pathText(tester), '/srv/work/sub');
      expect(seen, ['/srv/work', '/srv/work/sub']);
    });

    testWidgets('Enter with an empty value restores the current path without navigating', (tester) async {
      final api = CountingApi();
      await pumpPanel(tester, api);
      expect(api.lists, 1);
      await tester.enterText(pathField(), '   ');
      await tester.testTextInput.receiveAction(TextInputAction.go);
      await tester.pumpAndSettle();
      expect(pathText(tester), '/srv/work');
      expect(api.lists, 1);
    });

    testWidgets('Escape restores the current path without navigating', (tester) async {
      final api = CountingApi();
      await pumpPanel(tester, api);
      await tester.enterText(pathField(), '/tmp/xyz');
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await tester.pumpAndSettle();
      expect(pathText(tester), '/srv/work');
      expect(api.lists, 1);
    });

    testWidgets('blur restores uncommitted text and never commits', (tester) async {
      final api = CountingApi();
      await pumpPanel(tester, api);
      await tester.enterText(pathField(), '/tmp/xyz');
      await tester.pump();
      // enterText focuses the field; dropping focus must restore
      // the typed text, not navigate to it.
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pumpAndSettle();
      expect(pathText(tester), '/srv/work');
      expect(api.lists, 1);
    });

    testWidgets('focus puts the caret at the end of the path', (tester) async {
      await pumpPanel(tester, FakeApi());
      final field = tester.widget<TextField>(pathField());
      field.focusNode!.requestFocus();
      await tester.pump();
      final selection = tester.widget<TextField>(pathField()).controller!.selection;
      expect(selection, const TextSelection.collapsed(offset: 9));
    });
  });
}
