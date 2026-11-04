import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/models/blocks.dart';
import 'package:tmuxweb_client/widgets/block_view.dart';
import 'package:tmuxweb_client/widgets/mermaid_view.dart';
import 'package:tmuxweb_client/widgets/web_content_view.dart';

void main() {
  group('buildMermaidHtml', () {
    const source = 'graph TD\n  A --> B';
    late String html;

    setUp(() => html = buildMermaidHtml(source, 'http://chat.local:8899/'));

    test('loads the server vendored script, never a bundled copy', () {
      expect(html, contains('http://chat.local:8899/assets/mermaid/mermaid.min.js'));
      expect(html, isNot(contains('cdn')));
      expect(html, isNot(contains('unpkg')));
    });

    test('initializes exactly like web (index.html loadMermaid)', () {
      expect(html, contains('startOnLoad:false'));
      expect(html, contains("theme:'neutral'"));
      expect(html, contains("securityLevel:'strict'"));
      expect(html, contains('flowchart:{htmlLabels:false}'));
      expect(html, contains("fontFamily:'ui-sans-serif,system-ui'"));
    });

    test('renders into an SVG target and has an error container', () {
      expect(html, contains('id="mermaid-target"'));
      expect(html, contains('id="mermaid-error"'));
      expect(html, contains('mermaid 渲染失败：'));
      expect(html, contains("mermaid.render('mermaid-diagram'"));
    });

    test('reports render failures back to Flutter via the JS channel', () {
      expect(html, contains('window.MermaidError'));
      expect(html, contains('postMessage'));
    });

    test('handles a missing script as a render failure, not a blank page', () {
      expect(html, contains("typeof mermaid === 'undefined'"));
      expect(html, contains('无法加载 mermaid'));
    });

    test('neutralizes a diagram that tries to break out of the script block', () {
      final evil = buildMermaidHtml('graph TD\n  A["</script><script>alert(1)</script>"]', 'http://chat.local:8899');
      expect(evil, contains(r'<\/script>'));
      // The only literal </script> closers are the two real script tags.
      expect('</script>'.allMatches(evil), hasLength(2));
    });

    test('trims a trailing slash from the base URL', () {
      expect(mermaidScriptUrl('http://chat.local:8899/'), 'http://chat.local:8899/assets/mermaid/mermaid.min.js');
      expect(mermaidScriptUrl('http://chat.local:8899'), 'http://chat.local:8899/assets/mermaid/mermaid.min.js');
    });
  });

  group('parseCookieHeader', () {
    test('parses name=value pairs and keeps = inside values', () {
      expect(
        parseCookieHeader('tmux_web_session=abc=def; other=1'),
        {'tmux_web_session': 'abc=def', 'other': '1'},
      );
    });

    test('ignores empty and malformed pairs', () {
      expect(parseCookieHeader(''), isEmpty);
      expect(parseCookieHeader('nonsense; a=b'), {'a': 'b'});
    });
  });

  group('MermaidView without a platform view', () {
    testWidgets('shows a placeholder, never a platform view', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: MermaidView(source: 'graph TD\n  A --> B', assetBaseUrl: 'http://chat.local:8899', createPlatformView: false),
          ),
        ),
      );
      expect(find.byKey(const Key('mermaid-platform-placeholder')), findsOneWidget);
      expect(find.text('Mermaid 渲染视图'), findsOneWidget);
    });
  });

  group('BlockView mermaid toggle', () {
    const block = Block(kind: BlockKind.mermaid, source: 'graph TD\n  A --> B');

    testWidgets('renders by default and flips to source on 源码, back on 渲染', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: SingleChildScrollView(
              child: BlockView(block: block, baseUrl: 'http://chat.local:8899', createPlatformView: false),
            ),
          ),
        ),
      );
      expect(find.byKey(const Key('mermaid-platform-placeholder')), findsOneWidget);
      expect(find.text('源码'), findsOneWidget);

      await tester.tap(find.text('源码'));
      await tester.pump();
      expect(find.byKey(const Key('mermaid-platform-placeholder')), findsNothing);
      expect(find.text('graph TD\n  A --> B'), findsOneWidget);
      expect(find.text('渲染'), findsOneWidget);

      await tester.tap(find.text('渲染'));
      await tester.pump();
      expect(find.byKey(const Key('mermaid-platform-placeholder')), findsOneWidget);
    });

    testWidgets('without a base URL keeps the old source-only view', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: SingleChildScrollView(child: BlockView(block: block)),
          ),
        ),
      );
      expect(find.text('Mermaid 源码（客户端暂不渲染图形，可在网页版查看）'), findsOneWidget);
      expect(find.text('源码'), findsNothing);
      expect(find.text('graph TD\n  A --> B'), findsOneWidget);
    });

    testWidgets('on Windows keeps the old source-only view even with a base URL', (tester) async {
      debugDefaultTargetPlatformOverride = TargetPlatform.windows;
      addTearDown(() => debugDefaultTargetPlatformOverride = null);
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: SingleChildScrollView(
              child: BlockView(block: block, baseUrl: 'http://chat.local:8899', createPlatformView: false),
            ),
          ),
        ),
      );
      expect(find.text('Mermaid 源码（客户端暂不渲染图形，可在网页版查看）'), findsOneWidget);
      expect(find.text('源码'), findsNothing);
      expect(find.byKey(const Key('mermaid-platform-placeholder')), findsNothing);
      // Reset in the body, not only in tearDown: the test binding
      // verifies debug variables are unset before tearDowns run.
      debugDefaultTargetPlatformOverride = null;
    });

    testWidgets('table toggle behavior is unchanged', (tester) async {
      const table = Block(kind: BlockKind.table, source: '| a | b |\n| --- | --- |\n| 1 | 2 |');
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: SingleChildScrollView(child: BlockView(block: table)),
          ),
        ),
      );
      expect(find.text('1'), findsOneWidget);
      await tester.tap(find.text('源码'));
      await tester.pump();
      expect(find.text('| a | b |\n| --- | --- |\n| 1 | 2 |'), findsOneWidget);
      await tester.tap(find.text('渲染'));
      await tester.pump();
      expect(find.text('1'), findsOneWidget);
    });
  });
}
