import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/theme.dart';
import 'package:tmuxweb_client/widgets/message_tile.dart';
import 'package:tmuxweb_client/widgets/path_link_text.dart';
import 'package:tmuxweb_client/models/models.dart';

List<String> _links(String text) =>
    extractPathLinks(text).where((segment) => segment.isLink).map((segment) => segment.path!).toList();

List<String> _plain(String text) =>
    extractPathLinks(text).where((segment) => !segment.isLink).map((segment) => segment.text).toList();

void main() {
  group('extractPathLinks', () {
    test('absolute path after Chinese punctuation', () {
      expect(_links('看一下：/srv/work/notes.md，改一下'), ['/srv/work/notes.md']);
      expect(_plain('看一下：/srv/work/notes.md，改一下'), ['看一下：', '，改一下']);
    });

    test('home, dot and dot-dot prefixes count with one segment', () {
      expect(_links('在 ~/notes.md 里'), ['~/notes.md']);
      expect(_links('见 ./out.md'), ['./out.md']);
      expect(_links('在 ../shared/a.txt'), ['../shared/a.txt']);
    });

    test('relative paths need an extension', () {
      expect(_links('打开 results/out.md 看看'), ['results/out.md']);
      expect(_links('src/main.py 在这里'), ['src/main.py']);
      expect(_links('and/or 只是单词'), isEmpty);
      expect(_links('a/b 不是路径'), isEmpty);
    });

    test('single-segment absolute paths are not paths', () {
      expect(_links('看 /grid'), isEmpty);
      expect(_links('看 /OUT-OF-BAND'), isEmpty);
    });

    test('web prefixes are server routes, home is not', () {
      expect(_links('接口 /api/panes/%7/files/stat'), isEmpty);
      expect(_links('静态 /assets/app.js'), isEmpty);
      expect(_links('家目录 /home/mars/work/a.md'), ['/home/mars/work/a.md']);
    });

    test('URLs are skipped', () {
      expect(_links('见 https://example.com/a/b.md'), isEmpty);
      expect(_links('见 http://x.co/a/b'), isEmpty);
    });

    test('trailing punctuation is stripped', () {
      expect(_links('路径 /srv/work/a.md。'), ['/srv/work/a.md']);
      expect(_links('（/srv/work/a.md）'), ['/srv/work/a.md']);
      expect(_links('文件 /srv/work/a.md, 下一个'), ['/srv/work/a.md']);
    });

    test('no slash means no links', () {
      expect(_links('hello world'), isEmpty);
      expect(extractPathLinks('hello world'), hasLength(1));
    });
  });

  group('isTextPath / humanSize', () {
    test('text extensions preview, binaries do not', () {
      expect(isTextPath('/srv/a.md'), isTrue);
      expect(isTextPath('/srv/a.py'), isTrue);
      expect(isTextPath('/srv/a.yml'), isTrue);
      expect(isTextPath('/srv/a.png'), isFalse);
      expect(isTextPath('/srv/a.zip'), isFalse);
      expect(isTextPath('/srv/a'), isFalse);
    });

    test('human sizes match web human()', () {
      expect(humanSize(512), '512 B');
      expect(humanSize(2048), '2.0 KB');
      expect(humanSize(3 * 1024 * 1024), '3.0 MB');
    });
  });

  group('PathLinkText sheet', () {
    late List<String> resolved;
    late List<String> previews;
    late List<String> downloads;
    late int resolveCalls;

    PathLinkActions actions(Map<String, dynamic> stat) => PathLinkActions(
      resolve: (raw) async {
        resolveCalls += 1;
        resolved.add(raw);
        return stat;
      },
      onPreview: (path, _) => previews.add(path),
      onDownload: (path, _) => downloads.add(path),
      onOpenDir: (p) {},
    );

    setUp(() {
      resolved = [];
      previews = [];
      downloads = [];
      resolveCalls = 0;
    });

    Widget wrap(Widget child) => MaterialApp(theme: AppTheme.light(), home: Scaffold(body: child));

    testWidgets('tap stats once, offers Preview + Download for text, Preview does not download', (tester) async {
      await tester.pumpWidget(wrap(PathLinkText(
        text: '/srv/work/notes.md',
        actions: actions({'path': '/srv/work/notes.md', 'name': 'notes.md', 'exists': true, 'kind': 'file', 'size': 2048}),
      )));
      await tester.pumpAndSettle();

      await tester.tap(find.byType(PathLinkText));
      await tester.pumpAndSettle();
      expect(find.text('notes.md'), findsWidgets);
      expect(find.byKey(const Key('path-link-preview')), findsOneWidget);
      expect(find.byKey(const Key('path-link-download')), findsOneWidget);
      expect(resolveCalls, 1);

      await tester.tap(find.byKey(const Key('path-link-preview')));
      await tester.pumpAndSettle();
      expect(previews, ['/srv/work/notes.md']);
      expect(downloads, isEmpty);

      // Second tap reuses the cached stat: no second fileStat.
      await tester.tap(find.byType(PathLinkText));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('path-link-preview')), findsOneWidget);
      expect(resolveCalls, 1);
    });

    testWidgets('binary offers Download only, missing offers neither', (tester) async {
      await tester.pumpWidget(wrap(PathLinkText(
        text: '/srv/work/app.zip',
        actions: actions({'path': '/srv/work/app.zip', 'name': 'app.zip', 'exists': true, 'kind': 'file', 'size': 99}),
      )));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(PathLinkText));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('path-link-preview')), findsNothing);
      expect(find.byKey(const Key('path-link-download')), findsOneWidget);
      await tester.tap(find.byKey(const Key('path-link-download')));
      await tester.pumpAndSettle();
      expect(downloads, ['/srv/work/app.zip']);
    });

    testWidgets('directory offers open-dir only', (tester) async {
      await tester.pumpWidget(wrap(PathLinkText(
        text: '/srv/work/sub',
        actions: actions({'path': '/srv/work/sub', 'name': 'sub', 'exists': true, 'kind': 'dir', 'size': 0}),
      )));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(PathLinkText));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('path-link-open-dir')), findsOneWidget);
      expect(find.byKey(const Key('path-link-download')), findsNothing);
    });

    testWidgets('missing path reports not found and offers neither action', (tester) async {
      await tester.pumpWidget(wrap(PathLinkText(
        text: '/srv/work/gone.md',
        actions: actions({'path': '/srv/work/gone.md', 'name': 'gone.md', 'exists': false, 'kind': null, 'size': 0}),
      )));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(PathLinkText));
      await tester.pumpAndSettle();
      expect(find.text('找不到这个路径'), findsOneWidget);
      expect(find.byKey(const Key('path-link-preview')), findsNothing);
      expect(find.byKey(const Key('path-link-download')), findsNothing);
    });
  });

  group('MessageTile markdown path links', () {
    Widget wrap(Widget child) => MaterialApp(theme: AppTheme.light(), home: Scaffold(body: child));

    testWidgets('assistant markdown path is tappable and opens the sheet', (tester) async {
      var previews = <String>[];
      final actions = PathLinkActions(
        resolve: (raw) async => {'path': raw, 'name': 'notes.md', 'exists': true, 'kind': 'file', 'size': 10},
        onPreview: (path, _) => previews.add(path),
        onDownload: (p, s) {},
        onOpenDir: (p) {},
      );
      await tester.pumpWidget(wrap(MessageTile(
        message: ChatMessage(id: 'a', role: 'assistant', text: '/srv/work/notes.md'),
        pathLinks: actions,
      )));
      await tester.pumpAndSettle();

      // The whole paragraph is the link, so its center is the link.
      await tester.tap(find.byType(RichText).first);
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('path-link-preview')), findsOneWidget);
      await tester.tap(find.byKey(const Key('path-link-preview')));
      await tester.pumpAndSettle();
      expect(previews, ['/srv/work/notes.md']);
    });

    testWidgets('path inside a code span is not a link', (tester) async {
      final actions = PathLinkActions(
        resolve: (raw) async => {'path': raw, 'name': 'notes.md', 'exists': true, 'kind': 'file', 'size': 10},
        onPreview: (p, s) {},
        onDownload: (p, s) {},
        onOpenDir: (p) {},
      );
      await tester.pumpWidget(wrap(MessageTile(
        message: ChatMessage(id: 'a', role: 'assistant', text: '`/srv/work/notes.md`'),
        pathLinks: actions,
      )));
      await tester.pumpAndSettle();

      await tester.tap(find.byType(RichText).first);
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('path-link-preview')), findsNothing);
    });

    testWidgets('copy row shows the timestamp when the server sent one', (tester) async {
      await tester.pumpWidget(wrap(MessageTile(
        message: ChatMessage(id: 'u', role: 'user', text: 'hi', timestamp: '10:24'),
        pathLinks: null,
      )));
      await tester.pumpAndSettle();
      expect(find.text('10:24'), findsOneWidget);
      expect(find.text('复制全部'), findsOneWidget);
    });
  });
}
