import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/models/chat_source.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/screens/chat_screen.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart';
import 'package:tmuxweb_client/screens/shares_screen.dart';

import 'screens_test.dart' show FakeApi, wrap;

const _baseUrl = 'http://chat.local:8899';

void main() {
  late List<String> clipboard;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    clipboard = [];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      SystemChannels.platform,
      (call) async {
        if (call.method == 'Clipboard.setData') clipboard.add((call.arguments as Map)['text'] as String);
        return null;
      },
    );
  });

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, null);
  });

  Future<FakeApi> pumpShares(WidgetTester tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(SharesScreen(api: api, baseUrl: _baseUrl)));
    await tester.pumpAndSettle();
    return api;
  }

  testWidgets('shares list renders summary, cards, status and meta', (tester) async {
    await pumpShares(tester);

    expect(find.text('共 2 个分享 · 1 个有效'), findsOneWidget);
    expect(find.text('agent'), findsOneWidget);
    expect(find.text('review'), findsOneWidget);
    expect(find.text('有效'), findsOneWidget);
    expect(find.text('已停止'), findsOneWidget);
    expect(find.text('http://chat.local:8899/share/share-abc'), findsOneWidget);
    expect(find.text('路径：share-abc'), findsOneWidget);
    expect(find.text('Pane：abc~%0'), findsOneWidget);
    expect(find.textContaining('2030'), findsOneWidget);
    expect(find.text('密码保护'), findsOneWidget);
    expect(find.text('可对话'), findsOneWidget);
    expect(find.text('免密码'), findsOneWidget);
    expect(find.text('只读分享'), findsOneWidget);
    // Stopped shares get no extend/stop/password actions, like web.
    expect(find.byKey(const Key('share-extend-s2')), findsNothing);
    expect(find.byKey(const Key('share-stop-s2')), findsNothing);
    expect(find.byKey(const Key('share-password-s2')), findsNothing);
    expect(find.byKey(const Key('share-delete-s2')), findsOneWidget);
  });

  testWidgets('copy link writes the full URL and reports', (tester) async {
    await pumpShares(tester);

    await tester.tap(find.byKey(const Key('share-copy-s1')));
    await tester.pumpAndSettle();

    expect(clipboard, contains('http://chat.local:8899/share/share-abc'));
    expect(find.text('链接已复制'), findsOneWidget);
  });

  testWidgets('extend with 7 days records the seconds and reports', (tester) async {
    final api = await pumpShares(tester);

    await tester.tap(find.byKey(const Key('share-extend-s1')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('share-extend-seconds')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('增加 7 天').last);
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('share-extend-confirm')));
    await tester.pumpAndSettle();

    expect(api.extendedShares, contains('s1+604800'));
    expect(find.text('续期成功'), findsOneWidget);
  });

  testWidgets('reset password with an empty field clears protection', (tester) async {
    final api = await pumpShares(tester);

    await tester.tap(find.byKey(const Key('share-password-s1')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('share-password-save')));
    await tester.pumpAndSettle();

    expect(find.text('密码已更新'), findsOneWidget);
    expect(api.shares.firstWhere((share) => share['id'] == 's1')['password_protected'], isFalse);
    expect(find.text('免密码'), findsWidgets);
  });

  testWidgets('stop asks for confirmation then stops', (tester) async {
    await pumpShares(tester);

    await tester.tap(find.byKey(const Key('share-stop-s1')));
    await tester.pumpAndSettle();
    expect(find.text('立即停止后，访客将无法继续访问。确定吗？'), findsOneWidget);
    await tester.tap(find.widgetWithText(TextButton, '提前停止'));
    await tester.pumpAndSettle();

    expect(find.text('分享已停止'), findsOneWidget);
    expect(find.byKey(const Key('share-stop-s1')), findsNothing);
    expect(find.text('已停止'), findsWidgets);
  });

  testWidgets('delete asks for confirmation then deletes', (tester) async {
    await pumpShares(tester);

    await tester.tap(find.byKey(const Key('share-delete-s1')));
    await tester.pumpAndSettle();
    expect(find.text('永久删除这条分享记录？此操作不可撤销。'), findsOneWidget);
    await tester.tap(find.widgetWithText(TextButton, '删除记录'));
    await tester.pumpAndSettle();

    expect(find.text('记录已删除'), findsOneWidget);
    expect(find.text('共 1 个分享 · 0 个有效'), findsOneWidget);
  });

  testWidgets('open pushes the share screen', (tester) async {
    await pumpShares(tester);

    await tester.tap(find.byKey(const Key('share-open-s1')));
    await tester.pumpAndSettle();

    expect(find.byType(ShareScreen), findsOneWidget);
  });

  testWidgets('create share from chat copies the full link', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(ChatScreen(
      source: PaneChatSource(
        api: api,
        pane: Pane(ref: 'abc~%0', title: 'agent', command: 'hermes', hostId: 'abc'),
      ),
      baseUrl: _baseUrl,
    )));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('chat-share')));
    await tester.pumpAndSettle();
    expect(find.text('分享会话'), findsOneWidget);
    await tester.enterText(find.byKey(const Key('share-slug')), 'my-share');
    await tester.tap(find.byKey(const Key('share-submit')));
    await tester.pumpAndSettle();

    expect(find.text('已创建并复制：http://chat.local:8899/share/my-share'), findsOneWidget);
    expect(clipboard, contains('http://chat.local:8899/share/my-share'));
    expect(api.createdShares.map((share) => share['slug']), contains('my-share'));
  });
  testWidgets('expiry time includes seconds, like web toLocaleString', (tester) async {
    await pumpShares(tester);
    final label = tester.widget<Text>(find.textContaining('到期：').first).data!;
    expect(label, matches(RegExp(r'\d{2}:\d{2}:\d{2}$')));
  });

  testWidgets('empty title stays empty instead of falling back to slug', (tester) async {
    final api = FakeApi();
    api.shares.add({
      'id': 's3',
      'slug': 'share-empty',
      'url': '/share/share-empty',
      'pane_id': '%7',
      'title': '',
      'created_at': 1761868800,
      'expires_at': 1893456000,
      'revoked': false,
      'password_protected': false,
      'allow_input': false,
      'status': 'active',
    });
    await tester.pumpWidget(wrap(SharesScreen(api: api, baseUrl: _baseUrl)));
    await tester.pumpAndSettle();
    // The slug still appears in the meta line ('路径：share-empty'),
    // but never as the bare title text.
    expect(find.text('share-empty'), findsNothing);
    expect(find.text('共 3 个分享 · 2 个有效'), findsOneWidget);
  });

  testWidgets('open still pushes the in-app share view', (tester) async {
    await pumpShares(tester);
    await tester.tap(find.byKey(const Key('share-open-s1')));
    await tester.pumpAndSettle();
    expect(find.byType(ShareScreen), findsOneWidget);
  });

}
