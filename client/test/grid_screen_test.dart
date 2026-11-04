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

  /// Double-tap on a cell header title (web's maximize gesture; the
  /// header maximize button is gone). Two quick taps with a short
  /// pump between them, inside the double-tap timeout, plus a pump
  /// past the recognizer's 40ms tap-tracker timer afterwards — ending
  /// a test with that timer still pending fails the framework's
  /// post-test timer check.
  Future<void> doubleTapTitle(WidgetTester tester, String text) async {
    await tester.tap(find.text(text));
    await tester.pump(const Duration(milliseconds: 50));
    await tester.tap(find.text(text));
    await tester.pump(const Duration(milliseconds: 100));
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
    // The second pane is added after this point, and its busy dot
    // pulses forever, so pumpAndSettle would never settle from here
    // on: plain pumps only once the busy cell is in the tree.
    await tester.tap(find.byKey(const Key('grid-add')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('grid-pick-abc~%0')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('grid-mode-abc~%0')));
    await tester.pump();
    await tester.pump();
    expect(find.text('codex · codex'), findsOneWidget);
    expect(find.text('main · hermes'), findsOneWidget);

    await doubleTapTitle(tester, 'codex · codex');
    expect(find.text('codex · codex'), findsOneWidget);
    expect(find.text('main · hermes'), findsNothing);

    await doubleTapTitle(tester, 'codex · codex');
    expect(find.text('main · hermes'), findsOneWidget);

    // Leave no terminal cell behind: its reconnect timer would
    // otherwise still be pending when the test framework verifies
    // timers after the tree is disposed.
    await tester.tap(find.byKey(const Key('grid-mode-abc~%0')));
    await tester.pump();
  });

  testWidgets('canvas layout stores canvas, tiles on tidy and undoes', (tester) async {
    await pumpGrid(tester);
    await addPane(tester, 'grid-pick-%7');

    await tester.tap(find.byKey(const Key('grid-layout-canvas')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('grid-tidy')), findsOneWidget);
    var saved = await savedGridJson();
    expect(saved['layout'], 'canvas');
    expect((saved['cells'] as Map).containsKey('%7'), isTrue);

    await tester.tap(find.byKey(const Key('grid-tidy')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('grid-undo')), findsOneWidget);

    await tester.tap(find.byKey(const Key('grid-undo')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('grid-undo')), findsNothing);
  });

  testWidgets('canvas drag moves the cell past the 4px dead zone and persists', (tester) async {
    await pumpGrid(tester);
    await addPane(tester, 'grid-pick-%7');
    await tester.tap(find.byKey(const Key('grid-layout-canvas')));
    await tester.pumpAndSettle();

    final before = await savedGridJson();
    final startX = ((before['cells'] as Map)['%7'] as Map)['x'] as num;
    await tester.drag(find.text('codex · codex'), const Offset(50, 30));
    await tester.pumpAndSettle();
    final after = await savedGridJson();
    final endX = ((after['cells'] as Map)['%7'] as Map)['x'] as num;
    expect(endX - startX, 50);
  });

  testWidgets('shape picker applies a grid shape and custom dialog persists a custom chip', (tester) async {
    await pumpGrid(tester);
    await addPane(tester, 'grid-pick-%7');
    await tester.tap(find.byKey(const Key('grid-layout-grid')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('grid-shape-picker')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('grid-shape-3x2')));
    await tester.pumpAndSettle();
    var saved = await savedGridJson();
    expect(saved['cols'], 3);
    expect(saved['rows'], 2);

    await tester.tap(find.byKey(const Key('grid-shape-picker')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('grid-shape-custom')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('grid-custom-cols')), '4');
    await tester.pump();
    await tester.enterText(find.byKey(const Key('grid-custom-rows')), '1');
    await tester.pump();
    await tester.tap(find.byKey(const Key('grid-custom-apply')));
    await tester.pumpAndSettle();
    saved = await savedGridJson();
    expect(saved['custom'], {'cols': 4, 'rows': 1});
    expect(saved['cols'], 4);
    expect(saved['rows'], 1);
    expect(find.text('4×1'), findsWidgets);
    expect(find.byKey(const Key('grid-size-custom')), findsOneWidget);
  });

  testWidgets('thinking chip shows for a busy pane, offline for a missing pane', (tester) async {
    await pumpGrid(tester);
    await addPane(tester, 'grid-pick-%7');
    await tester.tap(find.byKey(const Key('grid-add')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('grid-pick-abc~%0')));
    await tester.pump();
    await tester.pump();
    expect(find.text('思考中'), findsOneWidget);
  });

  testWidgets('offline chip shows for a pane missing from paneStates', (tester) async {
    SharedPreferences.setMockInitialValues({
      'tmux-grid-v1': jsonEncode({
        'panes': ['%7', 'ghost~%9'],
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
    expect(find.text('离线'), findsOneWidget);
  });

  testWidgets('layout switch preserves composer text via keyed cell reuse', (tester) async {
    await pumpGrid(tester);
    await addPane(tester, 'grid-pick-%7');
    await tester.enterText(find.byKey(const Key('chat-composer')), 'hello keep');
    await tester.pump();

    await tester.tap(find.byKey(const Key('grid-layout-grid')));
    await tester.pump();
    expect(find.text('hello keep'), findsOneWidget);

    await tester.tap(find.byKey(const Key('grid-layout-strip')));
    await tester.pump();
    expect(find.text('hello keep'), findsOneWidget);
  });

  testWidgets('open in console pushes the console, not a chat screen', (tester) async {
    await pumpGrid(tester);
    await addPane(tester, 'grid-pick-%7');
    await tester.tap(find.byKey(const Key('grid-open-%7')));
    await tester.pump();
    // Let the route transition finish in steps (a single big pump
    // does not reliably tick it): until then the grid route is
    // still onstage underneath the pushed console.
    for (var i = 0; i < 20; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    expect(find.text('会话'), findsWidgets);
    expect(find.byKey(const Key('grid-open-%7')), findsNothing);
  });

  testWidgets('narrow forces one column, hides layout switch and size bar, shows pager', (tester) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    SharedPreferences.setMockInitialValues({
      'tmux-grid-v1': jsonEncode({
        'panes': ['%7', 'ghost~%9'],
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
    expect(find.byKey(const Key('grid-layout-grid')), findsNothing);
    expect(find.text('2×1'), findsNothing);
    expect(find.text('1 / 2'), findsOneWidget);

    await tester.tap(find.byKey(const Key('grid-pager-down')));
    for (var i = 0; i < 8; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    expect(find.text('2 / 2'), findsOneWidget);
  });

  testWidgets('grid seam grip drags colFr live and persists on release', (tester) async {
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    SharedPreferences.setMockInitialValues({
      'tmux-grid-v1': jsonEncode({
        'panes': ['%7', 'ghost~%9'],
        'widths': {},
        'layout': 'grid',
        'cols': 2,
        'rows': 1,
        'colFr': [1, 1],
        'rowFr': [1],
        'modes': {},
        'explicit': {},
      }),
    });
    await pumpGrid(tester);
    expect(find.byKey(const Key('grid-grip-col-0-0')), findsOneWidget);
    await tester.drag(find.byKey(const Key('grid-grip-col-0-0')), const Offset(100, 0));
    await tester.pumpAndSettle();
    final saved = await savedGridJson();
    final colFr = (saved['colFr'] as List).map((value) => (value as num).toDouble()).toList();
    expect(colFr[0], greaterThan(1.0));
    expect(colFr[0] + colFr[1], closeTo(2.0, 0.01));
  });
}
