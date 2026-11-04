import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:path_provider_platform_interface/path_provider_platform_interface.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/screens/settings_screen.dart';
import 'package:tmuxweb_client/theme.dart';

import 'screens_test.dart' show FakeApi, wrap;

class FakePathProvider extends PathProviderPlatform {
  FakePathProvider(this.path);
  final String path;
  @override
  Future<String?> getApplicationDocumentsPath() async => path;
}

void main() {
  late PathProviderPlatform original;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    AppTheme.resetForTest();
    original = PathProviderPlatform.instance;
  });

  tearDown(() {
    AppTheme.resetForTest();
    PathProviderPlatform.instance = original;
  });

  Future<void> openSettings(WidgetTester tester, FakeApi api) async {
    await tester.pumpWidget(wrap(SettingsScreen(api: api, baseUrl: 'http://127.0.0.1:8787')));
    await tester.pumpAndSettle();
  }

  // The settings page scrolls, and its ListView builds children lazily:
  // a tile far below the fold does not exist yet, so ensureVisible
  // throws "No element", and scrollUntilVisible cannot name the
  // ListView (it wants a Scrollable, and TextFields each carry one).
  // So drag the ListView until the target exists, then ensureVisible.
  Future<void> scrollToVisible(WidgetTester tester, Finder finder) async {
    for (var i = 0; i < 20; i++) {
      if (finder.evaluate().isNotEmpty) {
        await tester.ensureVisible(finder);
        await tester.pumpAndSettle();
        return;
      }
      await tester.drag(find.byType(ListView), const Offset(0, -300));
      await tester.pumpAndSettle();
    }
    throw StateError('could not scroll to $finder');
  }

  Future<void> tapVisible(WidgetTester tester, Finder finder) async {
    await scrollToVisible(tester, finder);
    await tester.tap(finder);
    await tester.pumpAndSettle();
  }

  Future<void> enterVisible(WidgetTester tester, Finder finder, String text) async {
    await scrollToVisible(tester, finder);
    await tester.enterText(finder, text);
  }

  testWidgets('settings shows account info, plugin count and current theme', (tester) async {
    await openSettings(tester, FakeApi());
    expect(find.text('设置'), findsOneWidget);
    expect(find.text('服务器管理'), findsOneWidget);
    expect(find.text('已连接 2 台远程主机'), findsOneWidget);
    expect(find.text('1 / 2 已启用'), findsOneWidget);
    expect(find.text('默认'), findsWidgets);
  });

  testWidgets('password change succeeds and reports like web', (tester) async {
    await openSettings(tester, FakeApi());
    await tapVisible(tester, find.byKey(const Key('settings-password-tile')));
    await tester.pumpAndSettle();
    expect(find.textContaining('已自定义'), findsOneWidget);

    await enterVisible(tester, find.byKey(const Key('settings-pw-current')), 'old');
    await enterVisible(tester, find.byKey(const Key('settings-pw-new')), 'newpass123');
    await enterVisible(tester, find.byKey(const Key('settings-pw-confirm')), 'newpass123');
    await tapVisible(tester, find.byKey(const Key('settings-pw-submit')));
    await tester.pumpAndSettle();
    expect(find.text('密码已更新，其他设备需要重新登录；这个设备保持登录'), findsOneWidget);
  });

  testWidgets('password mismatch is caught locally, wrong current from the server', (tester) async {
    await openSettings(tester, FakeApi());
    await tapVisible(tester, find.byKey(const Key('settings-password-tile')));
    await tester.pumpAndSettle();

    await enterVisible(tester, find.byKey(const Key('settings-pw-current')), 'old');
    await enterVisible(tester, find.byKey(const Key('settings-pw-new')), 'newpass123');
    await enterVisible(tester, find.byKey(const Key('settings-pw-confirm')), 'different');
    await tapVisible(tester, find.byKey(const Key('settings-pw-submit')));
    await tester.pumpAndSettle();
    expect(find.text('两次输入的新密码不一致'), findsOneWidget);

    await enterVisible(tester, find.byKey(const Key('settings-pw-current')), 'wrong');
    await enterVisible(tester, find.byKey(const Key('settings-pw-new')), 'newpass123');
    await enterVisible(tester, find.byKey(const Key('settings-pw-confirm')), 'newpass123');
    await tapVisible(tester, find.byKey(const Key('settings-pw-submit')));
    await tester.pumpAndSettle();
    expect(find.text('当前密码不正确'), findsOneWidget);
  });

  testWidgets('theme selection applies live and syncs to the account', (tester) async {
    final api = FakeApi();
    await openSettings(tester, api);
    await tapVisible(tester, find.byKey(const Key('settings-themes-tile')));
    await tester.pumpAndSettle();

    await tapVisible(tester, find.byKey(const Key('theme-night')));
    await tester.pumpAndSettle();
    expect(AppTheme.name, 'night');
    expect(api.updatedTheme, 'night');
    expect(find.text('夜间'), findsWidgets);
  });

  testWidgets('plugin switch toggles and a failed plugin switch is disabled', (tester) async {
    final api = FakeApi();
    await openSettings(tester, api);
    await tapVisible(tester, find.byKey(const Key('settings-plugins-tile')));
    await tester.pumpAndSettle();

    expect(find.text('cross-server-relay'), findsOneWidget);
    expect(find.text('加载失败：入口模块不存在'), findsOneWidget);
    expect(tester.widget<Switch>(find.byKey(const Key('plugin-switch-broken-plugin'))).onChanged, isNull);

    await tapVisible(tester, find.byKey(const Key('plugin-switch-cross-server-relay')));
    await tester.pumpAndSettle();
    expect(api.pluginToggles, contains('cross-server-relay=false'));
    expect(tester.widget<Switch>(find.byKey(const Key('plugin-switch-cross-server-relay'))).value, isFalse);
  });

  testWidgets('export writes an encrypted file and import without a file warns', (tester) async {
    // NOTE: do not await an async dart:io call before the first pump
    // in a widget test — in this environment it leaves pumpAndSettle
    // waiting forever. Synchronous directory setup works fine.
    final temp = Directory('/tmp/tmuxweb-export-test');
    if (temp.existsSync()) temp.deleteSync(recursive: true);
    temp.createSync(recursive: true);
    PathProviderPlatform.instance = FakePathProvider(temp.path);
    await openSettings(tester, FakeApi());
    await tapVisible(tester, find.byKey(const Key('settings-io-tile')));
    await tester.pumpAndSettle();

    await enterVisible(tester, find.byKey(const Key('settings-export-password')), 'short');
    await tapVisible(tester, find.byKey(const Key('settings-export-hosts')));
    await tester.pumpAndSettle();
    expect(find.text('导出密码至少 6 位'), findsOneWidget);

    await enterVisible(tester, find.byKey(const Key('settings-export-password')), 'export123');
    await tapVisible(tester, find.byKey(const Key('settings-export-hosts')));
    await tester.pumpAndSettle();
    expect(find.textContaining('已加密导出 服务器配置（2 台主机）'), findsOneWidget);
    expect(find.textContaining('已保存到'), findsOneWidget);
    final files = temp.listSync(recursive: true).whereType<File>().toList();
    expect(files, hasLength(1));
    expect(files.single.path, contains('.tmuxweb.json'));

    await tapVisible(tester, find.byKey(const Key('settings-import-run')));
    await tester.pumpAndSettle();
    expect(find.text('先选择要导入的文件'), findsOneWidget);
    temp.deleteSync(recursive: true);
  });

  testWidgets('new-password field hints at least 6 characters', (tester) async {
    await openSettings(tester, FakeApi());
    await tapVisible(tester, find.byKey(const Key('settings-password-tile')));
    await tester.pumpAndSettle();
    expect(find.text('至少 6 位'), findsOneWidget);
  });

  testWidgets('preview-delay dropdown mirrors locally and sidebar memory toggles', (tester) async {
    await openSettings(tester, FakeApi());
    await tapVisible(tester, find.byKey(const Key('settings-display-tile')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('settings-preview-delay')), findsOneWidget);
    expect(find.byKey(const Key('settings-sidebar-memory')), findsOneWidget);
    expect(tester.widget<SwitchListTile>(find.byKey(const Key('settings-sidebar-memory'))).value, isFalse);

    await tapVisible(tester, find.byKey(const Key('settings-preview-delay')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('1 秒').last);
    await tester.pumpAndSettle();
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString('tmux-preview-delay'), '1');

    await tapVisible(tester, find.byKey(const Key('settings-sidebar-memory')));
    await tester.pumpAndSettle();
    expect(tester.widget<SwitchListTile>(find.byKey(const Key('settings-sidebar-memory'))).value, isTrue);
    expect(prefs.getString('tmux-sidebar-memory'), 'pane');
  });

  test('per-pane sidebar view storage caps at 60 panes, oldest trimmed', () async {
    SharedPreferences.setMockInitialValues({});
    for (var i = 0; i < 61; i++) {
      await SidebarViewMemory.save('pane-$i', 'files:/dir-$i');
    }
    final views = await SidebarViewMemory.load();
    expect(views.length, 60);
    expect(views.containsKey('pane-0'), isFalse);
    expect(views['pane-60'], 'files:/dir-60');
    await SidebarViewMemory.forget('pane-60');
    expect((await SidebarViewMemory.load()).containsKey('pane-60'), isFalse);
  });
}
