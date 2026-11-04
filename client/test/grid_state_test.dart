import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/models/grid_state.dart';

void main() {
  test('fromJson tolerates garbage and maps legacy layouts', () {
    final state = GridState.fromJson({
      'panes': ['%7', 42, null, 'abc~%0'],
      'widths': {'%7': 100, 'abc~%0': 400, 'gone': 500},
      'layout': 'grid3',
      'cols': 'not a number',
      'rows': 9,
      'modes': {'%7': 'term', 'abc~%0': 'chat'},
      'explicit': {'%7': true, 'abc~%0': false},
    });
    expect(state.panes, ['%7', 'abc~%0']);
    expect(state.widths, {'abc~%0': 400});
    expect(state.layout, 'grid');
    expect(state.cols, 3);
    expect(state.rows, 6);
    expect(state.modeFor('%7'), 'term');
    expect(state.modeFor('abc~%0'), 'chat');
    expect(state.explicit, {'%7'});
  });

  test('toJson round-trips', () {
    final state = GridState(
      panes: ['%7', 'abc~%0'],
      widths: {'%7': 300},
      layout: 'grid',
      cols: 3,
      rows: 2,
      colFr: [2, 1, 1],
      rowFr: [1, 1],
      modes: {'abc~%0': 'term'},
      explicit: {'abc~%0'},
    );
    final restored = GridState.fromJson(state.toJson());
    expect(restored.toJson(), state.toJson());
  });

  test('add ignores duplicates, remove drops widths, move reorders', () {
    final state = GridState();
    state.add('%7');
    state.add('%7');
    state.add('abc~%0');
    state.add('def~%1');
    expect(state.panes, ['%7', 'abc~%0', 'def~%1']);
    state.widths['%7'] = 300;
    state.remove('%7');
    expect(state.panes, ['abc~%0', 'def~%1']);
    expect(state.widths, isEmpty);
    state.move(1, 0);
    expect(state.panes, ['def~%1', 'abc~%0']);
    state.movePane('def~%1', 'abc~%0');
    expect(state.panes, ['abc~%0', 'def~%1']);
  });

  test('default modes follow transcripts unless chosen by hand', () {
    final state = GridState(panes: ['%7', 'abc~%0', 'def~%1']);
    state.setMode('def~%1', 'chat');
    state.applyDefaultModes({'%7': true, 'abc~%0': false, 'def~%1': false});
    expect(state.modeFor('%7'), 'chat');
    expect(state.modeFor('abc~%0'), 'term');
    // Hand-chosen chat wins over the transcript default.
    expect(state.modeFor('def~%1'), 'chat');
    // Unknown panes default to chat.
    state.add('ghost~%9');
    state.applyDefaultModes({'%7': true, 'abc~%0': false, 'def~%1': false});
    expect(state.modeFor('ghost~%9'), 'chat');
  });

  test('grid size resets ratios and rows grow with panes', () {
    final state = GridState();
    state.setGridSize(3, 2);
    expect(state.colFr, [1, 1, 1]);
    expect(state.rowFr, [1, 1]);
    for (var i = 0; i < 7; i++) {
      state.add('p$i');
    }
    expect(state.rows, 3);
  });

  test('presets snapshot and apply', () {
    final state = GridState(panes: ['%7'], layout: 'strip');
    state.setMode('%7', 'term');
    final snapshot = state.presetSnapshot();
    state.remove('%7');
    state.setLayout('grid');
    state.applyPreset(snapshot);
    expect(state.panes, ['%7']);
    expect(state.layout, 'strip');
    expect(state.modeFor('%7'), 'term');
    expect(state.explicit, contains('%7'));
    expect(state.maximized, isNull);
  });

  test('more than 12 panes grows the row count instead of throwing', () {
    // clamp(needed, 6) 在 needed > 6 时会抛 ArgumentError，而 fromJson 里的
    // 兜底会把这个异常吞掉，导致保存的布局整个消失。
    final state = GridState(cols: 2, rows: 6);
    expect(
      () {
        for (var i = 0; i < 13; i++) {
          state.add('pane-$i');
        }
      },
      returnsNormally,
    );
    expect(state.panes.length, 13);
    expect(state.rows, greaterThanOrEqualTo(7));
  });

  test('a stored 13-pane layout survives a round trip', () {
    final saved = GridState(cols: 2)
      ..add('a')
      ..add('b')
      ..add('c')
      ..add('d')
      ..add('e')
      ..add('f')
      ..add('g')
      ..add('h')
      ..add('i')
      ..add('j')
      ..add('k')
      ..add('l')
      ..add('m');
    final restored = GridState.fromJson(saved.toJson());
    expect(restored.panes.length, 13);
  });

  test('web-shaped JSON survives load-save structurally intact', () {
    final web = {
      'panes': ['%7', 'abc~%0'],
      'widths': {},
      'layout': 'canvas',
      'cols': 5,
      'rows': 2,
      'colFr': [1, 1, 1, 1, 1],
      'rowFr': [1, 1],
      'modes': {},
      'explicit': {},
      'custom': {'cols': 5, 'rows': 2},
      'cells': {
        '%7': {'x': 8, 'y': 8, 'w': 400, 'h': 300},
        'abc~%0': {'x': 416, 'y': 8, 'w': 400, 'h': 300},
      },
      'canvasCols': 5,
      'canvasRows': 2,
      'someFutureWebField': {'nested': [1, 2]},
    };
    final state = GridState.fromJson(web);
    expect(state.layout, 'canvas');
    expect(state.cols, 5);
    expect(state.custom, (cols: 5, rows: 2));
    expect(state.canvasCols, 5);
    expect(state.canvasRows, 2);
    expect(state.cells['%7'], isNotNull);
    final saved = state.toJson();
    expect(saved['layout'], 'canvas');
    expect(saved['cols'], 5);
    expect(saved['custom'], {'cols': 5, 'rows': 2});
    expect(saved['cells']['%7'], {'x': 8.0, 'y': 8.0, 'w': 400.0, 'h': 300.0});
    expect(saved['canvasCols'], 5);
    expect(saved['canvasRows'], 2);
    expect(saved['someFutureWebField'], {'nested': [1, 2]});
    // A second round-trip is stable.
    expect(GridState.fromJson(saved).toJson(), saved);
  });

  test('canvas is a real layout, unknown layouts fall back to strip, cols clamp 1-6', () {
    expect(GridState.fromJson({'layout': 'canvas'}).layout, 'canvas');
    expect(GridState.fromJson({'layout': 'mystery'}).layout, 'strip');
    expect(GridState.fromJson({'layout': 'grid', 'cols': 7}).cols, 6);
    expect(GridState.fromJson({'layout': 'grid', 'cols': 0}).cols, 1);
  });

  test('same-shape setGridSize keeps hand-dragged fr values', () {
    final state = GridState(layout: 'grid', cols: 2, rows: 2, colFr: [2, 1], rowFr: [1, 3]);
    state.setGridSize(2, 2);
    expect(state.colFr, [2, 1]);
    expect(state.rowFr, [1, 3]);
    state.setGridSize(3, 2);
    expect(state.colFr, [1, 1, 1]);
    expect(state.rowFr, [1, 1]);
  });

  test('bestShape prefers 2xN/3xN over vertical stacks', () {
    expect(GridState.bestShape(4, 1200, 900), (cols: 2, rows: 2));
    expect(GridState.bestShape(6, 1200, 900), (cols: 3, rows: 2));
    expect(GridState.bestShape(1, 1200, 900), (cols: 1, rows: 1));
    expect(GridState.bestShape(5, 400, 800), (cols: 1, rows: 5));
  });

  test('slotRect tiles from the top-left with an 8px gap', () {
    expect(GridState.slotRect(0, 2, 2, 1200, 900), const CanvasRect(x: 8, y: 8, w: 580, h: 438));
    expect(GridState.slotRect(1, 2, 2, 1200, 900).x, 8 + 580 + 8);
  });

  test('layout snapshot restores cells, shape and order', () {
    final state = GridState(panes: ['%7', 'abc~%0'], layout: 'canvas');
    state.tileCanvas(2, 1, 1200, 900);
    final snapshot = state.layoutSnapshot();
    state.cells['%7'] = const CanvasRect(x: 500, y: 500, w: 300, h: 200);
    state.canvasCols = 1;
    state.movePane('abc~%0', '%7');
    state.restoreLayoutSnapshot(snapshot);
    expect(state.cells['%7'], isNot(const CanvasRect(x: 500, y: 500, w: 300, h: 200)));
    expect(state.canvasCols, 2);
    expect(state.panes, ['%7', 'abc~%0']);
  });

  test('explicit chat entries in modes survive verbatim and track toggles', () {
    final state = GridState.fromJson({
      'panes': ['%7'],
      'modes': {'%7': 'chat'},
    });
    expect(state.modeFor('%7'), 'chat');
    expect(state.toJson()['modes'], {'%7': 'chat'});
    state.setMode('%7', 'term');
    expect(state.toJson()['modes'], {'%7': 'term'});
    state.setMode('%7', 'chat');
    expect(state.toJson()['modes'], {'%7': 'chat'});
  });

  test('presets carry canvas cells, shape and custom', () {
    final state = GridState(panes: ['%7'], layout: 'canvas');
    state.tileCanvas(2, 1, 1200, 900);
    state.custom = (cols: 2, rows: 1);
    final snapshot = state.presetSnapshot();
    state.remove('%7');
    state.setLayout('strip');
    state.applyPreset(snapshot);
    expect(state.layout, 'canvas');
    expect(state.cells['%7'], isNotNull);
    expect(state.custom, (cols: 2, rows: 1));
  });
}
