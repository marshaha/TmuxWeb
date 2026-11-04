import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/screens/plugin_page_screen.dart';
import 'package:tmuxweb_client/widgets/plugin_buttons.dart';

void main() {
  group('pluginEntryPath', () {
    test('strips the /plugin-assets/<name>/ prefix', () {
      expect(pluginEntryPath('/plugin-assets/demo/index.html'), 'index.html');
      expect(pluginEntryPath('/plugin-assets/demo/sub/page.html'), 'sub/page.html');
    });

    test('strips a leading slash from other absolute paths', () {
      expect(pluginEntryPath('/index.html'), 'index.html');
    });

    test('leaves relative paths alone', () {
      expect(pluginEntryPath('index.html'), 'index.html');
      expect(pluginEntryPath('sub/page.html'), 'sub/page.html');
    });
  });

  group('pluginPageUrl', () {
    test('resolves a relative entry against the plugin-assets base', () {
      expect(
        pluginPageUrl('http://chat.local:8899/', 'demo', 'index.html'),
        'http://chat.local:8899/plugin-assets/demo/index.html',
      );
    });

    test('passes absolute paths and absolute URLs through', () {
      expect(
        pluginPageUrl('http://chat.local:8899', 'demo', '/plugin-assets/demo/index.html'),
        'http://chat.local:8899/plugin-assets/demo/index.html',
      );
      expect(
        pluginPageUrl('http://chat.local:8899', 'demo', 'https://example.com/page.html'),
        'https://example.com/page.html',
      );
    });
  });

  group('pluginLoadErrorMessage', () {
    test('maps 404 to the disabled-or-missing message', () {
      expect(pluginLoadErrorMessage('加载失败：HTTP 404'), '插件已停用或页面不存在（404）');
    });

    test('maps 403 to the permission message and passes other text through', () {
      expect(pluginLoadErrorMessage('加载失败：HTTP 403'), '没有权限查看该插件页面（403）');
      expect(pluginLoadErrorMessage('加载失败：timeout'), '加载失败：timeout');
    });
  });

  group('PluginPageScreen without a platform view', () {
    testWidgets('shows title and placeholder, and 关闭 pops back', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: Center(
              child: Builder(
                builder: (context) => TextButton(
                  onPressed: () => Navigator.of(context).push(
                    MaterialPageRoute(
                      builder: (_) => const PluginPageScreen(
                        baseUrl: 'http://chat.local:8899',
                        pluginName: 'demo',
                        entryPath: 'index.html',
                        title: '演示插件',
                        createPlatformView: false,
                      ),
                    ),
                  ),
                  child: const Text('打开插件'),
                ),
              ),
            ),
          ),
        ),
      );
      await tester.tap(find.text('打开插件'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.text('演示插件'), findsOneWidget);
      expect(find.byKey(const Key('plugin-page-placeholder')), findsOneWidget);

      await tester.tap(find.byKey(const Key('plugin-page-close')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.byKey(const Key('plugin-page-placeholder')), findsNothing);
      expect(find.text('打开插件'), findsOneWidget);
    });
  });

  group('PluginButtons', () {
    const buttons = [
      {'plugin': 'demo', 'label': '演示', 'href': '/plugin-assets/demo/index.html', 'icon': 'plug', 'title': '演示插件'},
      {'plugin': 'other', 'label': '其它', 'href': 'panel.html', 'icon': '', 'title': ''},
    ];

    testWidgets('renders labels, icon glyphs and title tooltips, and navigates', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: PluginButtons(
              buttonsLoader: () async => buttons,
              baseUrl: 'http://chat.local:8899',
              createPlatformView: false,
            ),
          ),
        ),
      );
      await tester.pump();
      expect(find.text('演示'), findsOneWidget);
      expect(find.text('其它'), findsOneWidget);
      expect(find.byIcon(Icons.extension), findsOneWidget);
      expect(find.byTooltip('演示插件'), findsOneWidget);
      expect(find.byTooltip('其它'), findsOneWidget);

      await tester.tap(find.text('演示'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.text('演示插件'), findsOneWidget);
      expect(find.byKey(const Key('plugin-page-placeholder')), findsOneWidget);
    });

    testWidgets('empty buttons render nothing', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: PluginButtons(
              buttonsLoader: () async => const [],
              baseUrl: 'http://chat.local:8899',
              createPlatformView: false,
            ),
          ),
        ),
      );
      await tester.pump();
      expect(find.byType(PluginButtons), findsOneWidget);
      expect(find.byType(OutlinedButton), findsNothing);
    });

    testWidgets('a fetch failure renders nothing, like web', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: PluginButtons(
              buttonsLoader: () async => throw Exception('boom'),
              baseUrl: 'http://chat.local:8899',
              createPlatformView: false,
            ),
          ),
        ),
      );
      await tester.pump();
      expect(find.text('插件按钮加载失败，点击重试'), findsNothing);
      expect(find.byType(OutlinedButton), findsNothing);
    });
  });
}
