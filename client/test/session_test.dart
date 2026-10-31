import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/api/api_client.dart';
import 'package:tmuxweb_client/screens/chat_screen.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart';

import 'screens_test.dart' show FakeApi, wrap;

const _baseUrl = 'http://chat.local:8899';

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('new session dialog creates on the host and opens the pane', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(HostsScreen(api: api, baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('new-session-abc')));
    await tester.pumpAndSettle();
    expect(find.text('新建 tmux 会话'), findsOneWidget);
    expect(find.text('主机：ctyun'), findsOneWidget);
    await tester.enterText(find.byKey(const Key('session-name')), 'worker-1');
    await tester.tap(find.byKey(const Key('session-create')));
    await tester.pumpAndSettle();

    expect(api.createdSessions, [
      {'host': 'abc', 'name': 'worker-1', 'path': ''},
    ]);
    expect(find.byType(ChatScreen), findsOneWidget);
  });

  testWidgets('empty session name is caught in the dialog', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(HostsScreen(api: api, baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('new-session-localhost')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('session-create')));
    await tester.pump();

    expect(find.text('请填写会话名称'), findsOneWidget);
    expect(api.createdSessions, isEmpty);
  });

  testWidgets('failed creation reports in the dialog and stays open', (tester) async {
    final api = FakeApi(sessionError: ApiException(409, '会话已存在'));
    await tester.pumpWidget(wrap(HostsScreen(api: api, baseUrl: _baseUrl)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('new-session-localhost')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('session-name')), 'codex');
    await tester.tap(find.byKey(const Key('session-create')));
    await tester.pumpAndSettle();

    expect(find.textContaining('会话已存在'), findsOneWidget);
    expect(find.text('新建 tmux 会话'), findsOneWidget);
  });
}
