import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/screens/grid_screen.dart';

import 'screens_test.dart' show FakeApi, wrap;

Future<String?> savedGrid() async {
  final prefs = await SharedPreferences.getInstance();
  return prefs.getString('tmux-grid-v1');
}

Future<Map<String, dynamic>> savedGridJson() async {
  final raw = await savedGrid();
  return raw == null ? {} : Map<String, dynamic>.from(jsonDecode(raw) as Map);
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  Future<FakeApi> pumpGrid(WidgetTester tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(GridScreen(api: api, baseUrl: 'http://chat.local:8899')));
    await tester.pumpAndSettle();
    return api;
  }

  Future<void> addPane(WidgetTester tester, String key) async {
    await tester.tap(find.byKey(const Key('grid-add')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(Key(key)));
    await tester.pumpAndSettle();
  }

  testWidgets('empty state, add flow and persistence', (tester) async {
    await pumpGrid(tester);

    expect(find.text('还没有窗口'), findsOneWidget);
    await addPane(tester, 'grid-pick-%7');
    expect(find.text('codex · codex'), findsOneWidget);
    expect(find.text('2 个窗口'), findsNothing);
    expect(find.text('1 个窗口'), findsOneWidget);
    final saved = await savedGridJson();
    expect(saved['panes'], ['%7']);
  });

  testWidgets('imported grid state appears without adding', (tester) async {
    SharedPreferences.setMockInitialValues({
      'tmux-grid-v1': jsonEncode({
        'panes': ['%7'],
        'widths': {},
        'layout': 'strip',
        'cols': 2,
        'rows': 2,
        'colFr': [1, 1],
        'rowFr': [1, 1],
        'modes': {},
        'explicit': {},
      }),
    });
    await pumpGrid(tester);

    expect(find.text('codex · codex'), findsOneWidget);
  });

  testWidgets('remove drops the cell and the saved pane', (tester) async {
    await pumpGrid(tester);
    await addPane(tester, 'grid-pick-%7');

    await tester.tap(find.byKey(const Key('grid-remove-%7')));
    await tester.pumpAndSettle();

    expect(find.text('还没有窗口'), findsOneWidget);
    expect((await savedGridJson())['panes'], isEmpty);
  });

  testWidgets('layout switch shows size presets and stores the shape', (tester) async {
    await pumpGrid(tester);
    await addPane(tester, 'grid-pick-%7');

    await tester.tap(find.byKey(const Key('grid-layout-grid')));
    await tester.pumpAndSettle();
    expect(find.text('2×1'), findsOneWidget);
    await tester.tap(find.byKey(const Key('grid-size-3x2')));
    await tester.pumpAndSettle();
    final saved = await savedGridJson();
    expect(saved['layout'], 'grid');
    expect(saved['cols'], 3);
    expect(saved['rows'], 2);

    await tester.tap(find.byKey(const Key('grid-layout-strip')));
    await tester.pumpAndSettle();
    expect(find.text('2×1'), findsNothing);
  });

  testWidgets('mode toggle switches a cell between chat and terminal', (tester) async {
    await pumpGrid(tester);
    await addPane(tester, 'grid-pick-%7');

    expect(find.byTooltip('切到终端'), findsOneWidget);
    await tester.tap(find.byKey(const Key('grid-mode-%7')));
    await tester.pump();
    expect(find.byTooltip('切到聊天'), findsOneWidget);
    expect((await savedGridJson())['modes'], {'%7': 'term'});
    await tester.tap(find.byKey(const Key('grid-mode-%7')));
    await tester.pump();
    expect(find.byTooltip('切到终端'), findsOneWidget);
  });

  testWidgets('presets save, apply and delete', (tester) async {
    await pumpGrid(tester);
    await addPane(tester, 'grid-pick-%7');

    await tester.tap(find.byKey(const Key('grid-presets')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('grid-preset-name')), 'morning');
    await tester.tap(find.byKey(const Key('grid-preset-save')));
    await tester.pumpAndSettle();
    expect(find.text('morning'), findsOneWidget);
    expect(find.text('1 窗口 · 并排'), findsOneWidget);
    await tester.tap(find.text('关闭'));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('grid-remove-%7')));
    await tester.pumpAndSettle();
    expect(find.text('还没有窗口'), findsOneWidget);

    await tester.tap(find.byKey(const Key('grid-presets')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('grid-preset-apply-morning')));
    await tester.pumpAndSettle();
    expect(find.text('codex · codex'), findsOneWidget);

    await tester.tap(find.byKey(const Key('grid-presets')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('grid-preset-delete-morning')));
    await tester.pumpAndSettle();
    expect(find.text('morning'), findsNothing);
  });

  testWidgets('maximize hides the other cell and restores it', (tester) async {
    await pumpGrid(tester);
    await addPane(tester, 'grid-pick-%7');
    // The second pane defaults to a terminal cell (no transcript), so
    // switch it to chat with plain pumps: settling would chase the
    // terminal's reconnect timer forever.
    await tester.tap(find.byKey(const Key('grid-add')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('grid-pick-abc~%0')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('grid-mode-abc~%0')));
    await tester.pump();
    await tester.pumpAndSettle();
    expect(find.text('codex · codex'), findsOneWidget);
    expect(find.text('main · hermes'), findsOneWidget);

    await tester.tap(find.byKey(const Key('grid-max-%7')));
    await tester.pumpAndSettle();
    expect(find.text('codex · codex'), findsOneWidget);
    expect(find.text('main · hermes'), findsNothing);

    await tester.tap(find.byKey(const Key('grid-max-%7')));
    await tester.pumpAndSettle();
    expect(find.text('main · hermes'), findsOneWidget);
  });
}
