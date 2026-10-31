import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/screens/hosts_admin_screen.dart';

import 'screens_test.dart' show FakeApi, wrap;

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  test('RemoteHost parses jump, tmux path, enabled and position', () {
    final host = RemoteHost.fromJson({
      'id': 'x',
      'name': 'target',
      'hostname': '10.0.0.1',
      'port': 22,
      'username': 'u',
      'auth_type': 'password',
      'jump_host_id': 'abc',
      'jump_spec': '',
      'jump_path': 'bastion -> target',
      'tmux_path': '/usr/bin/tmux',
      'enabled': true,
      'position': 2,
    });
    expect(host.jumpHostId, 'abc');
    expect(host.jumpPath, 'bastion -> target');
    expect(host.tmuxPath, '/usr/bin/tmux');
    expect(host.enabled, isTrue);
    expect(host.position, 2);
    final bare = RemoteHost.fromJson({'id': 'y', 'name': 'n', 'hostname': 'h', 'port': 22, 'username': 'u'});
    expect(bare.jumpHostId, isEmpty);
    expect(bare.jumpPath, isEmpty);
    expect(bare.enabled, isTrue);
  });

  testWidgets('admin list shows move buttons and jump/tmux lines', (tester) async {
    await tester.pumpWidget(wrap(HostsAdminScreen(api: FakeApi())));
    await tester.pumpAndSettle();

    expect(find.text('ctyun'), findsOneWidget);
    expect(find.text('backup'), findsOneWidget);
    expect(find.text('经由 bastion'), findsOneWidget);
    expect(find.text('tmux: /usr/bin/tmux'), findsOneWidget);
    // First host cannot move up, last cannot move down, like web.
    expect(tester.widget<IconButton>(find.byKey(const Key('host-move-up-abc'))).onPressed, isNull);
    expect(tester.widget<IconButton>(find.byKey(const Key('host-move-down-def'))).onPressed, isNull);
    expect(tester.widget<IconButton>(find.byKey(const Key('host-move-down-abc'))).onPressed, isNotNull);
    expect(tester.widget<IconButton>(find.byKey(const Key('host-move-up-def'))).onPressed, isNotNull);
  });

  testWidgets('moving a host down posts the new order', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(HostsAdminScreen(api: api)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('host-move-down-abc')));
    await tester.pumpAndSettle();
    expect(api.reorderedIds, [
      ['def', 'abc'],
    ]);
  });

  testWidgets('editor tests the connection and saves the typed jump spec', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(HostsAdminScreen(api: api)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('host-add')));
    await tester.pumpAndSettle();

    // Fill the form: the first TextField is the name field, the fourth
    // (after hostname and port) is the username field, which has no key.
    final fields = find.byType(TextField);
    await tester.enterText(fields.at(0), 'target');
    await tester.enterText(find.byKey(const Key('host-hostname')), '10.0.0.5');
    await tester.enterText(fields.at(3), 'mars');
    await tester.enterText(find.byKey(const Key('host-jump-spec')), 'jumpuser@jump:2222');
    // Credential is the password field for the default password auth.
    await tester.enterText(find.byKey(const Key('host-credential')), 'secret');
    await tester.ensureVisible(find.byKey(const Key('host-test')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('host-test')));
    await tester.pumpAndSettle();

    expect(find.textContaining('联通成功：SSH 正常，发现 3 个 pane，session：main, codex'), findsOneWidget);
    expect(api.testedHostPayloads.single['jump_spec'], 'jumpuser@jump:2222');
    expect(api.testedHostPayloads.single['jump_host_id'], '');
    expect(api.testedHostPayloads.single['fingerprint'], 'SHA256:abc');

    await tester.ensureVisible(find.byKey(const Key('host-save')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('host-save')));
    await tester.pumpAndSettle();
    expect(api.savedHostPayloads.single['jump_spec'], 'jumpuser@jump:2222');
    expect(api.savedHostPayloads.single['jump_host_id'], '');
  });

  testWidgets('editor jump pick offers the other hosts, not itself', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(HostsAdminScreen(api: api)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('host-edit-abc')));
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.byKey(const Key('host-jump-host')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('host-jump-host')));
    await tester.pumpAndSettle();
    expect(find.text('直连（不经过跳板机）'), findsWidgets);
    expect(find.text('backup · 10.0.0.9'), findsOneWidget);
    expect(find.text('ctyun · 100.123.123.8'), findsNothing);
  });
}
