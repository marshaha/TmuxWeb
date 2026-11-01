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
}
