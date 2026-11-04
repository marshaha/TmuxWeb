import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart' show ShareScreen;
import 'package:tmuxweb_client/screens/plugin_page_screen.dart' show pluginLoadErrorMessage;
import 'package:tmuxweb_client/screens/shares_screen.dart' show SharesScreen, shareOpenUrl;
import 'package:tmuxweb_client/widgets/plugin_buttons.dart' show PluginButtons;

import 'screens_test.dart' show FakeApi, wrap;

const _baseUrl = 'http://chat.local:8899';

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
  });

  group('plugin buttons: web parity for loading and failure (items 12+13)', () {
    const buttons = [
      {'plugin': 'demo', 'label': '演示', 'href': '/plugin-assets/demo/index.html', 'icon': 'plug', 'title': '演示插件'},
    ];

    Widget buttonsWidget({required Future<List<Map<String, dynamic>>> Function() loader}) =>
        MaterialApp(
          home: Scaffold(
            body: PluginButtons(
              buttonsLoader: loader,
              baseUrl: _baseUrl,
              createPlatformView: false,
            ),
          ),
        );

    testWidgets('renders nothing while loading: no loading text at all', (tester) async {
      // A pending loader, not awaited IO: the widget must render nothing
      // while the fetch is in flight — web mounts nothing until success.
      final gate = Completer<List<Map<String, dynamic>>>();
      await tester.pumpWidget(buttonsWidget(loader: () => gate.future));
      await tester.pump();

      expect(find.byType(PluginButtons), findsOneWidget);
      expect(find.text('加载中…'), findsNothing);
      expect(find.text('插件按钮加载失败，点击重试'), findsNothing);
      expect(find.byType(OutlinedButton), findsNothing);

      gate.complete(const []);
      await tester.pump();
      expect(find.byType(OutlinedButton), findsNothing);
    });

    testWidgets('renders nothing on API failure: no retry widget, no error text', (tester) async {
      // Web's plugins-ui.js swallows a failed /api/plugins fetch and
      // mounts nothing; the old retry affordance is gone by design.
      await tester.pumpWidget(buttonsWidget(loader: () async => throw Exception('boom')));
      await tester.pump();

      expect(find.byType(PluginButtons), findsOneWidget);
      expect(find.text('插件按钮加载失败，点击重试'), findsNothing);
      expect(find.text('加载中…'), findsNothing);
      expect(find.textContaining('失败'), findsNothing);
      expect(find.byType(OutlinedButton), findsNothing);
    });

    testWidgets('a failed fetch stays dropped: no later retry affordance appears', (tester) async {
      await tester.pumpWidget(buttonsWidget(loader: () async => throw Exception('boom')));
      await tester.pump();
      await tester.pump(const Duration(seconds: 1));

      expect(find.text('插件按钮加载失败，点击重试'), findsNothing);
      expect(find.byType(OutlinedButton), findsNothing);
    });

    testWidgets('successful buttons still render after the revert', (tester) async {
      await tester.pumpWidget(buttonsWidget(loader: () async => buttons));
      await tester.pump();

      expect(find.text('演示'), findsOneWidget);
      expect(find.byTooltip('演示插件'), findsOneWidget);
    });
  });

  group('shares open URL construction (item 11)', () {
    const share = {'url': '/share/share-abc', 'slug': 'share-abc'};

    test('absolute path resolves against the base, like web new URL(url, origin)', () {
      expect(shareOpenUrl(_baseUrl, share), 'http://chat.local:8899/share/share-abc');
    });

    test('a trailing-slash base does not produce a double slash', () {
      expect(shareOpenUrl('http://chat.local:8899/', share), 'http://chat.local:8899/share/share-abc');
    });

    test('absolute http(s) URLs pass through untouched', () {
      expect(
        shareOpenUrl(_baseUrl, {'url': 'https://example.com/share/x', 'slug': 'x'}),
        'https://example.com/share/x',
      );
    });

    test('relative paths resolve under the base', () {
      expect(
        shareOpenUrl(_baseUrl, {'url': 'share/rel', 'slug': 'rel'}),
        'http://chat.local:8899/share/rel',
      );
    });

    test('an empty base leaves the URL as-is', () {
      expect(shareOpenUrl('', share), '/share/share-abc');
    });
  });

  group('shares open branch selection (item 11)', () {
    testWidgets('on native (flutter test: kIsWeb is false) open pushes the in-app share view', (tester) async {
      // Platform equivalent, stated plainly: native targets have no
      // browser tabs, so 打开 shows the same share inside the app.
      final api = FakeApi();
      await tester.pumpWidget(wrap(SharesScreen(api: api, baseUrl: _baseUrl)));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('share-open-s1')));
      await tester.pumpAndSettle();

      expect(find.byType(ShareScreen), findsOneWidget);
    });

    testWidgets('a wired tab opener is NOT called on native: the gate needs kIsWeb too', (tester) async {
      // The web branch is `kIsWeb && tabOpener != null`. Under flutter
      // test kIsWeb is false, so even a wired opener must not fire here;
      // the in-app view is used instead. The real new-tab call therefore
      // cannot run under flutter test at all (no browser window, and
      // package:web does not compile on the VM) — it needs a web-target
      // build once the conditional-import barrel wiring tabOpener lands.
      final opened = <String>[];
      final api = FakeApi();
      await tester.pumpWidget(wrap(SharesScreen(api: api, baseUrl: _baseUrl, tabOpener: opened.add)));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('share-open-s1')));
      await tester.pumpAndSettle();

      expect(opened, isEmpty);
      expect(find.byType(ShareScreen), findsOneWidget);
    });
  });

  group('web content view preflight error state (item 10)', () {
    // LIMIT, stated honestly: WebContentView's web implementation
    // (client/lib/widgets/web_content_view_web.dart) cannot be
    // instantiated under flutter test — its file imports dart:ui_web
    // and package:web, which do not compile on the VM (verified: a probe
    // import fails to load). So the iframe-gating branch itself (no
    // iframe element before a successful preflight; error state on 404)
    // is NOT covered by a VM widget test here. What IS covered is the
    // user-visible end of that chain: the preflight reports failures
    // through onError with the same message shape as the native
    // onHttpError path, and PluginPageScreen maps those messages to the
    // error screen below. The gating seam that a Chrome-run test can
    // exercise is WebContentView's `preflight` constructor parameter
    // (PluginAssetPreflight): inject `(_) async => 404` and assert
    // onError fires and no iframe is built.
    test('preflight 404 maps to the disabled-or-missing error state', () {
      expect(pluginLoadErrorMessage('加载失败：HTTP 404'), '插件已停用或页面不存在（404）');
    });

    test('preflight 403 maps to the permission error state', () {
      expect(pluginLoadErrorMessage('加载失败：HTTP 403'), '没有权限查看该插件页面（403）');
    });

    test('a failed preflight check surfaces its message unchanged', () {
      expect(pluginLoadErrorMessage('加载失败：无法连接服务器'), '加载失败：无法连接服务器');
    });
  });
}
