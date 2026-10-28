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

  testWidgets('tapping a markdown file previews it', (tester) async {
    await tester.pumpWidget(wrap(FilesScreen(api: FakeApi(), pane: pane)));
    await tester.pumpAndSettle();

    await tester.tap(find.text('notes.md'));
    await tester.pumpAndSettle();
    expect(find.text('标题'), findsWidgets);
  });
}
