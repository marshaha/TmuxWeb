import 'package:flutter/material.dart';
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
}
