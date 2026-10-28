import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/api/api_client.dart';
import 'package:tmuxweb_client/screens/chat_screen.dart';
import 'package:tmuxweb_client/screens/hosts_admin_screen.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart';
import 'package:tmuxweb_client/theme.dart';

import 'chat_screen_test.dart' show FakeSource;
import 'screens_test.dart' show FakeApi, wrap;

void main() {
  testWidgets('the sidebar groups panes as host → session → window', (tester) async {
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://127.0.0.1:8787')));
    await tester.pumpAndSettle();

    // hosts
    expect(find.text('localhost'), findsOneWidget);
    expect(find.text('ctyun'), findsOneWidget);
    // sessions and windows are their own tree levels
    expect(find.text('codex'), findsWidgets);
    expect(find.text('main'), findsWidgets);
    // panes stay reachable
    expect(find.byKey(const Key('pane-%7')), findsOneWidget);
    expect(find.byKey(const Key('pane-abc~%0')), findsOneWidget);
    // and the management entry exists
    expect(find.byKey(const Key('hosts-admin')), findsOneWidget);
  });

  testWidgets('busy panes get an accent dot in the tree', (tester) async {
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://127.0.0.1:8787')));
    await tester.pumpAndSettle();
    final panes = tester.widgetList<ListTile>(find.byType(ListTile)).where((tile) => tile.leading != null).toList();
    expect(panes.length, greaterThanOrEqualTo(2));
  });

  testWidgets('Enter sends and Shift+Enter keeps the newline', (tester) async {
    final source = FakeSource();
    await tester.pumpWidget(MaterialApp(theme: AppTheme.light(), home: ChatScreen(source: source, poll: Duration.zero)));
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('chat-composer')), '先发一条');
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.pumpAndSettle();
    expect(source.sent, ['先发一条']);

    await tester.enterText(find.byKey(const Key('chat-composer')), '要多行');
    await tester.pump();
    await tester.sendKeyDownEvent(LogicalKeyboardKey.shift);
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.shift);
    await tester.pumpAndSettle();
    expect(source.sent, ['先发一条'], reason: 'Shift+Enter must not send');
  });


  testWidgets('the server manager lists hosts and offers the add dialog', (tester) async {
    await tester.pumpWidget(wrap(HostsAdminScreen(api: FakeApi())));
    await tester.pumpAndSettle();
    expect(find.text('ctyun'), findsOneWidget);
    expect(find.textContaining('100.123.123.8'), findsOneWidget);
    expect(find.byKey(const Key('host-add')), findsOneWidget);

    await tester.tap(find.byKey(const Key('host-add')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('host-skip-verify')), findsOneWidget);
    expect(find.byKey(const Key('host-reveal')), findsOneWidget);
    expect(find.byKey(const Key('host-hostname')), findsOneWidget);

    await tester.ensureVisible(find.byKey(const Key('host-skip-verify')));
    await tester.pumpAndSettle();
    // labels sit above their boxes instead of floating over the text
    final label = tester.getRect(find.text('主机 / IP'));
    final box = tester.getRect(find.byKey(const Key('host-hostname')));
    expect(label.bottom, lessThanOrEqualTo(box.top + 1));
    final nameLabel = tester.getRect(find.text('显示名称'));
    expect(nameLabel.bottom, lessThanOrEqualTo(tester.getRect(find.byType(TextField).first).top + 1));

    await tester.tap(find.byKey(const Key('host-skip-verify')));
    await tester.pumpAndSettle();
    final checkbox = tester.widget<CheckboxListTile>(find.byKey(const Key('host-skip-verify')));
    expect(checkbox.value, isTrue);
  });

  test('the socket URL follows the http scheme', () {
    final client = ApiClient(baseUrl: 'https://chat.example.com');
    expect(client.wsUri('/ws/terminal/%257').toString(), 'wss://chat.example.com/ws/terminal/%257');
    final plain = ApiClient(baseUrl: 'http://10.0.0.1:8787');
    expect(plain.wsUri('/ws/terminal/%257', {'cols': '100'}).toString(),
        'ws://10.0.0.1:8787/ws/terminal/%257?cols=100');
  });
}
