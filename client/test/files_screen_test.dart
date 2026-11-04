import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/screens/files_screen.dart';

import 'screens_test.dart' show FakeApi, wrap;

void main() {
  final pane = Pane(ref: '%7', title: 'review', command: 'codex', session: 'codex', path: '/srv/work');

  testWidgets('the file panel lists a directory with sizes', (tester) async {
    await tester.pumpWidget(wrap(FilesScreen(api: FakeApi(), pane: pane)));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('file-notes.md')), findsOneWidget);
    expect(find.byKey(const Key('file-sub')), findsOneWidget);
    expect(find.text('2.0 KB'), findsOneWidget);
    expect(find.textContaining('/srv/work'), findsWidgets);
    // actions for upload / zip are available
    expect(find.byKey(const Key('files-upload')), findsOneWidget);
    expect(find.byKey(const Key('files-zip')), findsOneWidget);
  });

  testWidgets('tapping a folder opens it and the up button returns', (tester) async {
    await tester.pumpWidget(wrap(FilesScreen(api: FakeApi(), pane: pane)));
    await tester.pumpAndSettle();

    await tester.tap(find.text('sub'));
    await tester.pumpAndSettle();
    final path = tester.widget<TextField>(find.byKey(const Key('files-path'))).controller!.text;
    expect(path, '/srv/work/sub');

    await tester.tap(find.byKey(const Key('files-up')));
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(find.byKey(const Key('files-path'))).controller!.text, '/srv');
  });

  testWidgets('rename goes through the API and refreshes the listing', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(FilesScreen(api: api, pane: pane)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('file-more-notes.md')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('file-action-rename')));
    await tester.pumpAndSettle();

    final field = find.byKey(const Key('file-rename-input'));
    expect(field, findsOneWidget);
    await tester.enterText(field, 'readme.md');
    await tester.tap(find.byKey(const Key('file-rename-ok')));
    await tester.pumpAndSettle();

    expect(api.renames, ['/srv/work/notes.md -> readme.md']);
  });

  testWidgets('deleting a folder asks first and can be recursive', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(FilesScreen(api: api, pane: pane)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('file-more-sub')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('file-action-delete')));
    await tester.pumpAndSettle();

    // 目录默认带「连同目录内容一起删除」，取消不应调用接口
    expect(find.byKey(const Key('file-delete-recursive')), findsOneWidget);
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
    expect(api.deletes, isEmpty);

    await tester.tap(find.byKey(const Key('file-more-sub')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('file-action-delete')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('file-delete-ok')));
    await tester.pumpAndSettle();

    expect(api.deletes, ['/srv/work/sub|recursive=true']);
  });

  testWidgets('tapping a markdown file previews it', (tester) async {
    await tester.pumpWidget(wrap(FilesScreen(api: FakeApi(), pane: pane)));
    await tester.pumpAndSettle();

    await tester.tap(find.text('notes.md'));
    await tester.pumpAndSettle();
    expect(find.text('标题'), findsWidgets);
  });

  testWidgets('checkbox selection shows the footer with batch actions', (tester) async {
    await tester.pumpWidget(wrap(FilesScreen(api: FakeApi(), pane: pane)));
    await tester.pumpAndSettle();

    expect(find.text('已选 1 项'), findsNothing);
    await tester.tap(find.byKey(const Key('file-check-notes.md')));
    await tester.pumpAndSettle();
    expect(find.text('已选 1 项'), findsOneWidget);
    expect(find.byKey(const Key('files-footer-rename')), findsOneWidget);
    expect(find.byKey(const Key('files-footer-delete')), findsOneWidget);
    expect(find.byKey(const Key('files-footer-zip')), findsOneWidget);

    await tester.tap(find.byKey(const Key('files-select-all')));
    await tester.pumpAndSettle();
    expect(find.text('已选 2 项'), findsOneWidget);
    // Web hides footer rename unless exactly one is picked.
    expect(find.byKey(const Key('files-footer-rename')), findsNothing);

    await tester.tap(find.byKey(const Key('files-select-none')));
    await tester.pumpAndSettle();
    expect(find.text('已选 1 项'), findsNothing);
    expect(find.byKey(const Key('files-footer-delete')), findsNothing);
  });

  testWidgets('footer batch delete previews both names and deletes once', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(FilesScreen(api: api, pane: pane)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('file-check-notes.md')));
    await tester.tap(find.byKey(const Key('file-check-sub')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('files-footer-delete')));
    await tester.pumpAndSettle();

    expect(find.text('删除 2 项'), findsOneWidget);
    expect(find.text('notes.md、sub'), findsOneWidget);
    await tester.tap(find.byKey(const Key('file-delete-ok')));
    await tester.pumpAndSettle();

    expect(api.deletes, ['/srv/work/notes.md,/srv/work/sub|recursive=true']);
  });

  testWidgets('footer rename renames the single pick', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(FilesScreen(api: api, pane: pane)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('file-check-notes.md')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('files-footer-rename')));
    await tester.pumpAndSettle();

    final field = find.byKey(const Key('file-rename-input'));
    expect(field, findsOneWidget);
    // 16px rename input: web's mobile rule against iOS auto-zoom.
    expect(tester.widget<TextField>(field).style?.fontSize, 16);
    await tester.enterText(field, 'readme.md');
    await tester.tap(find.byKey(const Key('file-rename-ok')));
    await tester.pumpAndSettle();

    expect(api.renames, ['/srv/work/notes.md -> readme.md']);
  });

  testWidgets('F2 renames and Delete deletes the selection', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(FilesScreen(api: api, pane: pane)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('file-check-notes.md')));
    await tester.pumpAndSettle();
    await tester.sendKeyEvent(LogicalKeyboardKey.f2);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('file-rename-input')), findsOneWidget);
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();

    await tester.sendKeyEvent(LogicalKeyboardKey.delete);
    await tester.pumpAndSettle();
    expect(find.text('删除'), findsWidgets);
    await tester.tap(find.byKey(const Key('file-delete-ok')));
    await tester.pumpAndSettle();
    expect(api.deletes, ['/srv/work/notes.md|recursive=false']);
  });

  testWidgets('right-click opens a menu with the same actions', (tester) async {
    await tester.pumpWidget(wrap(FilesScreen(api: FakeApi(), pane: pane)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('file-notes.md')), buttons: kSecondaryButton);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('file-context-rename')), findsOneWidget);
    await tester.tap(find.byKey(const Key('file-context-rename')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('file-rename-input')), findsOneWidget);
  });

  testWidgets('directory row menu offers 打包下载', (tester) async {
    // saveFileBytes writes under getApplicationDocumentsDirectory;
    // point that channel at /tmp so the zip save succeeds in tests.
    const channel = MethodChannel('plugins.flutter.io/path_provider');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      channel,
      (call) async => '/tmp',
    );
    addTearDown(() => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null));
    await tester.pumpWidget(wrap(FilesScreen(api: FakeApi(), pane: pane)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('file-more-sub')));
    await tester.pumpAndSettle();
    expect(find.text('打包下载'), findsOneWidget);
    await tester.tap(find.byKey(const Key('file-action-zipdir')));
    await tester.pumpAndSettle();
    // Zip of the directory row goes through the same save path; the
    // fake API returns PK bytes, so a save snackbar appears.
    expect(find.textContaining('已保存到'), findsOneWidget);
  });
}
