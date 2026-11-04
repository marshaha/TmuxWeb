import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/screens/grid_screen.dart';
import 'package:tmuxweb_client/widgets/grid_pager.dart';

import 'screens_test.dart' show FakeApi, wrap;

/// FakeApi with overridable pane states and recent/pins payloads, so
/// each test controls exactly what the status sweep and the drawer
/// recent block see.
class _GridParityApi extends FakeApi {
  Map<String, String> states = {'%7': 'idle', 'abc~%0': 'busy'};
  Map<String, dynamic> pinsData = {'recent': [], 'pinned': []};

  @override
  Future<Map<String, String>> paneStates() async => states;

  @override
  Future<Map<String, dynamic>> pins() async => pinsData;
}

Map<String, dynamic> _gridJson(List<String> panes,
        {String layout = 'strip', Map<String, dynamic>? cells}) =>
    {
      'panes': panes,
      'widths': {},
      'layout': layout,
      'cols': 2,
      'rows': 2,
      'colFr': [1, 1],
      'rowFr': [1, 1],
      'modes': {},
      'explicit': {},
      if (cells != null) 'cells': cells,
    };

Future<Map<String, dynamic>> _savedGridJson() async {
  final prefs = await SharedPreferences.getInstance();
  final raw = prefs.getString('tmux-grid-v1');
  return raw == null ? {} : Map<String, dynamic>.from(jsonDecode(raw) as Map);
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  Future<_GridParityApi> pumpGrid(WidgetTester tester,
      {Map<String, dynamic>? saved,
      Map<String, String>? states,
      Map<String, dynamic>? pinsData,
      bool settle = true}) async {
    final api = _GridParityApi();
    if (states != null) api.states = states;
    if (pinsData != null) api.pinsData = pinsData;
    if (saved != null) SharedPreferences.setMockInitialValues({'tmux-grid-v1': jsonEncode(saved)});
    await tester.pumpWidget(wrap(GridScreen(api: api, baseUrl: 'http://chat.local:8899')));
    if (settle) {
      await tester.pumpAndSettle();
    } else {
      // A busy pane renders the repeating _BusyDot pulse, which
      // schedules frames forever, so pumpAndSettle can never
      // return; a few plain pumps still let the async loads land.
      await tester.pump();
      await tester.pump();
      await tester.pump();
    }
    return api;
  }

  testWidgets('grid drawer mounts RecentBlock with the chat-bubble console entry', (tester) async {
    await pumpGrid(tester,
        saved: _gridJson(['%7']),
        pinsData: {
          'recent': [
            {'pane': 'other~%1', 'title': 'other session', 'visited_at': 0}
          ],
          'pinned': [],
        });

    tester.state<ScaffoldState>(find.byType(Scaffold).first).openDrawer();
    await tester.pumpAndSettle();

    // The shared block, not a new sidebar: its list and rows render.
    expect(find.byKey(const Key('recent-list')), findsOneWidget);
    expect(find.text('other session'), findsOneWidget);
    // Web e353ff8: the console entry is the #i-chat bubble titled
    // 聊天（控制台）, never the old #i-console terminal glyph.
    final console = tester.widget<IconButton>(find.byKey(const Key('grid-drawer-console')));
    expect(console.tooltip, '聊天（控制台）');
    expect((console.icon as Icon).icon, Icons.chat_bubble_outline);

    // Web side-foot hint: tapping a session adds it to the grid.
    // The new cell's header shows the recent row's title (grid
    // _loadNames reads it as the pane's label), so the cell is
    // pinned down by its remove button key, not by the raw ref.
    await tester.tap(find.text('other session'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('grid-remove-other~%1')), findsOneWidget);
    expect((await _savedGridJson())['panes'], ['%7', 'other~%1']);
  });

  testWidgets('grid drawer console entry opens the console, keeping the grid underneath', (tester) async {
    await pumpGrid(tester, saved: _gridJson(['%7']));
    tester.state<ScaffoldState>(find.byType(Scaffold).first).openDrawer();
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('grid-drawer-console')));
    for (var i = 0; i < 20; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    expect(find.byKey(const Key('grid-open-%7')), findsNothing);
    expect(find.text('会话'), findsWidgets);
  });

  testWidgets('canvas resize handles exist and resize a cell', (tester) async {
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await pumpGrid(tester,
        // Idle states: a busy pane's repeating pulse would keep
        // pumpAndSettle from ever returning (see pumpGrid).
        states: {'%7': 'idle', 'abc~%0': 'idle'},
        saved: _gridJson(['%7', 'abc~%0'], layout: 'canvas', cells: {
          '%7': {'x': 8, 'y': 8, 'w': 400, 'h': 300},
          'abc~%0': {'x': 420, 'y': 8, 'w': 400, 'h': 300},
        }));

    for (final dir in ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']) {
      expect(find.byKey(Key('canvas-handle-%7-$dir')), findsOneWidget, reason: 'missing $dir handle');
    }

    // SE drag by (60,40): right 408 -> 468, bottom 308 -> 348, far
    // from every snap target, so the size moves exactly by the drag.
    await tester.drag(find.byKey(const Key('canvas-handle-%7-se')), const Offset(60, 40));
    await tester.pumpAndSettle();
    final saved = await _savedGridJson();
    expect((saved['cells']['%7'] as Map)['w'], 460);
    expect((saved['cells']['%7'] as Map)['h'], 340);
  });

  testWidgets('canvas resize snaps to a neighbour edge and shows a guide after 140ms', (tester) async {
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await pumpGrid(tester,
        states: {'%7': 'idle', 'abc~%0': 'idle'},
        saved: _gridJson(['%7', 'abc~%0'], layout: 'canvas', cells: {
          '%7': {'x': 8, 'y': 8, 'w': 400, 'h': 300},
          'abc~%0': {'x': 420, 'y': 8, 'w': 400, 'h': 300},
        }));

    // Right edge 408 -> 418 lands within web's SNAP=6 of the
    // neighbour's left edge (420), so it snaps to 420 (w 412).
    final gesture = await tester.startGesture(tester.getCenter(find.byKey(const Key('canvas-handle-%7-se'))));
    await gesture.moveBy(const Offset(10, 0));
    await tester.pump();
    // Guides wait out web's GUIDE_DELAY=140ms: not yet painted.
    await tester.pump(const Duration(milliseconds: 50));
    expect(find.byKey(const Key('canvas-guide-x-420')), findsNothing);
    expect(find.text('412×300'), findsOneWidget);
    await tester.pump(const Duration(milliseconds: 100));
    expect(find.byKey(const Key('canvas-guide-x-420')), findsOneWidget);
    await gesture.up();
    await tester.pumpAndSettle();
    final saved = await _savedGridJson();
    expect((saved['cells']['%7'] as Map)['w'], 412);
    // Guides and badge clear on release, like web's finish routine.
    expect(find.byKey(const Key('canvas-guide-x-420')), findsNothing);
    expect(find.byKey(const Key('canvas-badge')), findsNothing);
  });

  testWidgets('pager long-press fires at 380ms, not before', (tester) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final positions = <Offset>[];
    await tester.pumpWidget(wrap(Scaffold(
      body: Stack(
        children: [
          GridPager(
            index: 1,
            total: 2,
            onUp: () {},
            onDown: () {},
            onPositionChanged: (left, top) => positions.add(Offset(left, top)),
          ),
        ],
      ),
    )));
    await tester.pumpAndSettle();
    Offset center() => tester.getCenter(find.byKey(const Key('grid-pager-text')));

    // Release well before the hold: never enters drag mode.
    var gesture = await tester.startGesture(center());
    await tester.pump(const Duration(milliseconds: 200));
    await gesture.moveBy(const Offset(30, 20));
    await tester.pump();
    await gesture.up();
    await tester.pump(const Duration(milliseconds: 500));
    expect(positions, isEmpty);

    // Move and release at 379ms: still before web's 380ms hold.
    gesture = await tester.startGesture(center());
    await tester.pump(const Duration(milliseconds: 379));
    await gesture.moveBy(const Offset(30, 20));
    await tester.pump();
    await gesture.up();
    await tester.pump(const Duration(milliseconds: 500));
    expect(positions, isEmpty);

    // Hold past 380ms, then move, then release: drag persists.
    gesture = await tester.startGesture(center());
    await tester.pump(const Duration(milliseconds: 380));
    await gesture.moveBy(const Offset(50, 40));
    await tester.pump();
    await gesture.up();
    await tester.pump(const Duration(milliseconds: 500));
    expect(positions, hasLength(1));
    final positioned = tester.widget<Positioned>(
        find.ancestor(of: find.byKey(const Key('grid-pager-text')), matching: find.byType(Positioned)).first);
    expect(positioned.left, isNotNull);
    expect(positioned.top, isNotNull);
    expect(positions.single, Offset(positioned.left!, positioned.top!));
  });

  testWidgets('thinking chip: explicit offline shows, missing never shows', (tester) async {
    // Explicit 'offline' in the sweep -> 离线 (web statusText).
    await pumpGrid(tester, saved: _gridJson(['%7']), states: {'%7': 'offline'});
    expect(find.text('离线'), findsOneWidget);
  });

  testWidgets('thinking chip: pane missing from a non-empty sweep is not offline', (tester) async {
    // Web literal rule: a missing pane yields undefined from the
    // sweep map, so statusText falls through to '' — the old
    // Flutter guard (missing from non-empty sweep => offline) is
    // exactly what was reverted.
    await pumpGrid(tester, saved: _gridJson(['%7', 'ghost~%9']));
    expect(find.text('离线'), findsNothing);
  });

  testWidgets('thinking chip: empty sweep shows no offline chip', (tester) async {
    await pumpGrid(tester, saved: _gridJson(['%7', 'ghost~%9']), states: {});
    expect(find.text('离线'), findsNothing);
  });

  testWidgets('thinking chip: busy still shows', (tester) async {
    await pumpGrid(tester, saved: _gridJson(['abc~%0']), settle: false);
    expect(find.text('思考中'), findsOneWidget);
  });
}
