import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/screens/terminal_screen.dart';

import 'screens_test.dart' show FakeApi, wrap;

/// Terminal tests use the fake API's refused loopback socket
/// (ws://127.0.0.1:1): no live socket is ever required. They assert
/// status text, the live dot, the key-row toggle and font-size
/// persistence.
///
/// One deliberate exception to the usual no-real-IO-in-tests rule:
/// a refused socket is real dart:io async, which the test fake-async
/// zone does not deliver on its own. The tests below therefore drive
/// it with [WidgetTester.runAsync] real delays interleaved with
/// pumps — the standard flutter_test escape hatch — instead of
/// restructuring the screen's socket layer for tests.
void main() {
  final pane = Pane(ref: '%7', title: 'review', command: 'codex', session: 'codex', path: '/srv/work');

  setUp(() {
    SharedPreferences.setMockInitialValues({'tmux-term-font': 13.0});
  });

  /// Let a pending real socket failure land, then render it. The
  /// failure event needs two pumps to surface in the test zone
  /// (verified empirically: one pump leaves the status at 连接中…).
  Future<void> settleSocket(WidgetTester tester) async {
    await tester.runAsync(() async {
      await Future<void>.delayed(const Duration(milliseconds: 300));
    });
    await tester.pump();
    await tester.pump();
  }

  /// Drive the retry loop to its end state: each pass advances the
  /// fake timers, then delivers that attempt's refused connect.
  /// Steps of 2000ms: the retry delays grow (1200/2400/3600/4800/
  /// 6000ms), so smaller steps need more passes than the bound
  /// allows. Bounded so a regression fails instead of hanging
  /// (6 attempts total: initial + 5 retries).
  Future<void> driveToFailure(WidgetTester tester) async {
    for (var i = 0; i < 15 && find.text('连接失败').evaluate().isEmpty; i++) {
      await tester.pump(const Duration(milliseconds: 2000));
      await settleSocket(tester);
    }
    expect(find.text('连接失败'), findsOneWidget);
  }

  testWidgets('refused socket exhausts 5 retries and ends at 连接失败', (tester) async {
    await tester.pumpWidget(wrap(TerminalScreen(api: FakeApi(), pane: pane)));
    await driveToFailure(tester);

    expect(find.byKey(const Key('terminal-status')), findsOneWidget);
    expect(find.byKey(const Key('terminal-dot')), findsOneWidget);
  });

  testWidgets('reconnect steps through 重连中 N/5 before failing', (tester) async {
    await tester.pumpWidget(wrap(TerminalScreen(api: FakeApi(), pane: pane)));
    await settleSocket(tester);
    expect(find.text('重连中 1/5'), findsOneWidget);

    await tester.pump(const Duration(milliseconds: 1200));
    await settleSocket(tester);
    expect(find.text('重连中 2/5'), findsOneWidget);
  });

  testWidgets('embedded view shows the same status, with no AppBar', (tester) async {
    await tester.pumpWidget(wrap(TerminalScreen(api: FakeApi(), pane: pane, embedded: true)));
    await driveToFailure(tester);

    expect(find.byKey(const Key('terminal-dot')), findsOneWidget);
    expect(find.byType(AppBar), findsNothing);
  });

  testWidgets('key-row toggle flips visibility either way', (tester) async {
    await tester.pumpWidget(wrap(TerminalScreen(api: FakeApi(), pane: pane)));
    await tester.pumpAndSettle();

    final initiallyVisible = find.text('Esc').evaluate().isNotEmpty;
    await tester.tap(find.byKey(const Key('terminal-keys-toggle')));
    await tester.pumpAndSettle();
    expect(find.text('Esc').evaluate().isNotEmpty, isNot(initiallyVisible));
    await tester.tap(find.byKey(const Key('terminal-keys-toggle')));
    await tester.pumpAndSettle();
    expect(find.text('Esc').evaluate().isNotEmpty, initiallyVisible);
  });

  testWidgets('font +/- persists 9-22px to SharedPreferences', (tester) async {
    await tester.pumpWidget(wrap(TerminalScreen(api: FakeApi(), pane: pane)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('terminal-font-up')));
    await tester.pump();
    expect((await SharedPreferences.getInstance()).getDouble(terminalFontPrefsKey), 14.0);

    for (var i = 0; i < 10; i++) {
      await tester.tap(find.byKey(const Key('terminal-font-down')));
      await tester.pump();
    }
    // Clamped at web's 9px floor, not driven below it.
    expect((await SharedPreferences.getInstance()).getDouble(terminalFontPrefsKey), 9.0);
  });
}
