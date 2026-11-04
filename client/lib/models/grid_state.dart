import 'dart:math' as math;

/// Multi-window grid state, mirroring web/grid.html's localStorage
/// model so the two surfaces read and write the same shape:
/// `tmux-grid-v1` for the arrangement, `tmux-grid-presets-v1` for
/// named snapshots. The settings export/import already moves these
/// keys verbatim, so an imported grid lands here without conversion.
///
/// Non-destructive round-trip rules (web parity, grid.html:662):
/// web-only fields (`custom`, `cells`, `canvasCols`, `canvasRows`) and
/// any other unknown key are kept verbatim in [extras] and re-emitted
/// by [toJson], so a load→save cycle never clobbers them. `canvas` is
/// a real layout (not coerced to strip), and columns run 1-6 like
/// web's SHAPE_MAX_COLS (grid.html:396).
class CanvasRect {
  const CanvasRect({required this.x, required this.y, required this.w, required this.h});

  final double x;
  final double y;
  final double w;
  final double h;

  factory CanvasRect.fromJson(Map<String, dynamic> json) => CanvasRect(
        x: double.tryParse('${json['x']}') ?? 0,
        y: double.tryParse('${json['y']}') ?? 0,
        w: double.tryParse('${json['w']}') ?? 240,
        h: double.tryParse('${json['h']}') ?? 160,
      );

  Map<String, dynamic> toJson() => {'x': x, 'y': y, 'w': w, 'h': h};

  CanvasRect rounded() => CanvasRect(x: x.roundToDouble(), y: y.roundToDouble(), w: w.roundToDouble(), h: h.roundToDouble());

  @override
  bool operator ==(Object other) =>
      other is CanvasRect && other.x == x && other.y == y && other.w == w && other.h == h;

  @override
  int get hashCode => Object.hash(x, y, w, h);
}

class GridState {
  GridState({
    List<String>? panes,
    Map<String, double>? widths,
    this.layout = 'strip',
    this.cols = 2,
    this.rows = 2,
    List<double>? colFr,
    List<double>? rowFr,
    Map<String, String>? modes,
    Set<String>? explicit,
    this.maximized,
    Map<String, CanvasRect>? cells,
    this.canvasCols = 0,
    this.canvasRows = 0,
    this.custom,
    Map<String, dynamic>? extras,
  })  : panes = panes ?? [],
        widths = widths ?? {},
        colFr = colFr ?? [],
        rowFr = rowFr ?? [],
        modes = modes ?? {},
        explicit = explicit ?? {},
        cells = cells ?? {},
        extras = extras ?? {} {
    _applyGridShape();
  }

  static const gridKey = 'tmux-grid-v1';
  static const presetsKey = 'tmux-grid-presets-v1';

  /// Strip-mode minimum column width, like web's MIN.
  static const minStripWidth = 260.0;

  /// Canvas minimum cell size, like web's CANVAS_MIN_W/H.
  static const canvasMinWidth = 240.0;
  static const canvasMinHeight = 160.0;

  /// Grid size presets offered in the toolbar, like web's GRID_PRESETS.
  static const gridSizes = [
    (2, 1),
    (2, 2),
    (3, 2),
    (2, 3),
    (3, 3),
  ];

  /// Keys web persists in tmux-grid-v1 that this class models directly.
  /// Everything else in a saved document lands in [extras] verbatim.
  static const _knownKeys = {
    'panes',
    'widths',
    'layout',
    'cols',
    'rows',
    'colFr',
    'rowFr',
    'modes',
    'explicit',
    'custom',
    'cells',
    'canvasCols',
    'canvasRows',
  };

  final List<String> panes;
  final Map<String, double> widths;
  String layout;
  int cols;
  int rows;
  List<double> colFr;
  List<double> rowFr;

  /// Only 'term' entries are stored; chat is the absence of an entry,
  /// exactly like web's modes map. Exception: when a web document
  /// carried explicit 'chat' entries, those stay verbatim in
  /// extras['modes'] (kept in sync by setMode/applyDefaultModes) so a
  /// round-trip does not silently drop them.
  final Map<String, String> modes;

  /// Panes whose mode was chosen by hand and must survive the
  /// transcript-based defaulting when re-added.
  final Set<String> explicit;

  /// Transient: never persisted, like web's max.
  String? maximized;

  /// Free-positioned canvas cells, web's `cells` map (canvas pixels).
  final Map<String, CanvasRect> cells;

  /// Canvas tiling shape, web's canvasCols/canvasRows (0 = unset).
  int canvasCols;
  int canvasRows;

  /// Last custom shape from the shape picker, web's `custom`.
  ({int cols, int rows})? custom;

  /// Opaque web-only/unknown fields from the saved document, re-emitted
  /// verbatim by toJson. Never interpreted here.
  final Map<String, dynamic> extras;

  bool get isGrid => layout == 'grid';
  bool get isCanvas => layout == 'canvas';

  /// Rows actually laid out: at least enough for every pane, like
  /// web's applyGridShape.
  int get layoutRows => isGrid ? rows : 1;

  int get layoutCols => isGrid ? cols : 1;

  String modeFor(String pane) => modes[pane] == 'term' ? 'term' : 'chat';

  factory GridState.fromJson(Map<String, dynamic> json) {
    final rawPanes = json['panes'];
    final panes = rawPanes is List ? rawPanes.whereType<String>().toList() : <String>[];
    var layout = '${json['layout'] ?? 'strip'}';
    var cols = 2;
    if (layout == 'grid3') {
      layout = 'grid';
      cols = 3;
    } else if (layout == 'grid2') {
      layout = 'grid';
    } else if (layout != 'grid' && layout != 'canvas') {
      layout = 'strip';
    }
    final parsedCols = int.tryParse('${json['cols']}');
    if (parsedCols != null) cols = parsedCols;
    final parsedRows = int.tryParse('${json['rows']}');
    final widths = <String, double>{};
    final rawWidths = json['widths'];
    if (rawWidths is Map) {
      for (final entry in rawWidths.entries) {
        final width = double.tryParse('${entry.value}');
        if (panes.contains(entry.key) && width != null && width >= minStripWidth) {
          widths['${entry.key}'] = width;
        }
      }
    }
    final modes = <String, String>{};
    final rawModes = json['modes'];
    if (rawModes is Map) {
      for (final entry in rawModes.entries) {
        if (entry.value == 'term') modes['${entry.key}'] = 'term';
      }
    }
    final explicit = <String>{};
    final rawExplicit = json['explicit'];
    if (rawExplicit is Map) {
      for (final entry in rawExplicit.entries) {
        if (entry.value == true) explicit.add('${entry.key}');
      }
    }
    final cells = <String, CanvasRect>{};
    final rawCells = json['cells'];
    if (rawCells is Map) {
      for (final entry in rawCells.entries) {
        if (entry.value is Map) {
          final rect = CanvasRect.fromJson(Map<String, dynamic>.from(entry.value as Map));
          cells['${entry.key}'] = CanvasRect(
            x: rect.x,
            y: rect.y,
            w: math.max(canvasMinWidth, rect.w),
            h: math.max(canvasMinHeight, rect.h),
          );
        }
      }
    }
    final canvasCols = int.tryParse('${json['canvasCols']}') ?? 0;
    final canvasRows = int.tryParse('${json['canvasRows']}') ?? 0;
    ({int cols, int rows})? custom;
    final rawCustom = json['custom'];
    if (rawCustom is Map) {
      final c = int.tryParse('${rawCustom['cols']}');
      final r = int.tryParse('${rawCustom['rows']}');
      if (c != null && r != null && c >= 1 && r >= 1) {
        custom = (cols: c.clamp(1, 6), rows: r.clamp(1, 6));
      }
    }
    // Everything web persists that this class does not model stays
    // verbatim, so load→save never clobbers it. `modes` joins extras
    // only when it carried non-term entries (see the modes comment).
    final extras = <String, dynamic>{};
    for (final entry in json.entries) {
      if (_knownKeys.contains(entry.key)) continue;
      extras[entry.key] = _deepCopy(entry.value);
    }
    if (rawModes is Map && rawModes.values.any((value) => value != 'term')) {
      extras['modes'] = _deepCopy(rawModes);
    }
    final state = GridState(
      panes: panes,
      widths: widths,
      layout: layout,
      cols: cols,
      rows: parsedRows ?? 2,
      colFr: _frList(json['colFr']),
      rowFr: _frList(json['rowFr']),
      modes: modes,
      explicit: explicit,
      cells: cells,
      canvasCols: canvasCols,
      canvasRows: canvasRows,
      custom: custom,
      extras: extras,
    );
    return state;
  }

  static dynamic _deepCopy(dynamic value) {
    if (value is Map) {
      return {for (final entry in value.entries) '${entry.key}': _deepCopy(entry.value)};
    }
    if (value is List) return value.map(_deepCopy).toList();
    return value;
  }

  static List<double> _frList(dynamic raw) {
    if (raw is! List) return [];
    return raw.map((value) => double.tryParse('$value') ?? 1).toList();
  }

  Map<String, dynamic> toJson() {
    final out = <String, dynamic>{
      'panes': List<String>.from(panes),
      'widths': Map<String, double>.from(widths),
      'layout': layout,
      'cols': cols,
      'rows': rows,
      'colFr': List<double>.from(colFr),
      'rowFr': List<double>.from(rowFr),
      'modes': Map<String, String>.from(modes),
      'explicit': {for (final pane in explicit) pane: true},
      'custom': custom == null ? null : {'cols': custom!.cols, 'rows': custom!.rows},
      'cells': {for (final entry in cells.entries) entry.key: entry.value.toJson()},
      'canvasCols': canvasCols,
      'canvasRows': canvasRows,
    };
    // Extras override their key (notably a verbatim 'modes' map that
    // carried explicit 'chat' entries); unknown keys are appended.
    for (final entry in extras.entries) {
      out[entry.key] = _deepCopy(entry.value);
    }
    return out;
  }

  void _applyGridShape() {
    cols = cols.clamp(1, 6);
    final needed = panes.isEmpty ? 1 : (panes.length / cols).ceil();
    // 下界不能超过上界：pane 多到需要超过 6 行时 clamp(needed, 6) 会抛 ArgumentError，
    // 从 fromJson 走这条路还会被静默吞掉，整个已保存的布局消失。
    rows = rows.clamp(needed, needed > 6 ? needed : 6);
    colFr = _fitFr(colFr, cols);
    rowFr = _fitFr(rowFr, rows);
  }

  static List<double> _fitFr(List<double> list, int count) {
    final out = list.take(count).map((value) => value.isFinite && value >= 0.1 ? value : 1.0).toList();
    while (out.length < count) {
      out.add(1);
    }
    return out;
  }

  void add(String pane) {
    if (pane.isEmpty || panes.contains(pane)) return;
    panes.add(pane);
    _applyGridShape();
  }

  void remove(String pane) {
    panes.remove(pane);
    widths.remove(pane);
    cells.remove(pane);
    if (maximized == pane) maximized = null;
    _applyGridShape();
  }

  void move(int from, int to) {
    if (from < 0 || from >= panes.length || to < 0 || to >= panes.length || from == to) return;
    final pane = panes.removeAt(from);
    panes.insert(to, pane);
  }

  void movePane(String pane, String before) {
    final from = panes.indexOf(pane);
    final to = panes.indexOf(before);
    if (from < 0 || to < 0 || from == to) return;
    // Web's drop semantics: remove, then insert at the target's
    // original index (so dropping A onto the next cell B swaps them).
    panes.removeAt(from);
    panes.insert(to.clamp(0, panes.length), pane);
  }

  void _syncRawModes(String pane, String mode) {
    final raw = extras['modes'];
    if (raw is Map) raw[pane] = mode;
  }

  void setMode(String pane, String mode) {
    if (mode == 'term') {
      modes[pane] = 'term';
    } else {
      modes.remove(pane);
    }
    _syncRawModes(pane, mode == 'term' ? 'term' : 'chat');
    explicit.add(pane);
  }

  /// Defaulting from pane metadata, like web's applyDefaultModes: a
  /// pane without a transcript degrades to the terminal, unless the
  /// user chose its mode by hand.
  void applyDefaultModes(Map<String, bool> hasTranscriptByPane) {
    var changed = false;
    for (final pane in panes) {
      if (explicit.contains(pane)) continue;
      final hasTranscript = hasTranscriptByPane[pane];
      final want = hasTranscript == false ? 'term' : 'chat';
      if (modeFor(pane) == want) continue;
      if (want == 'term') {
        modes[pane] = 'term';
      } else {
        modes.remove(pane);
      }
      _syncRawModes(pane, want);
      changed = true;
    }
    if (changed) _applyGridShape();
  }

  void setLayout(String next) {
    layout = next == 'grid' || next == 'canvas' ? next : 'strip';
    _applyGridShape();
  }

  void setGridSize(int nextCols, int nextRows) {
    // Web grid.html:887-897: re-clicking the current shape must not
    // reset hand-dragged fr values back to equal shares.
    final same = layout == 'grid' && cols == nextCols && rows == nextRows;
    layout = 'grid';
    cols = nextCols;
    rows = nextRows;
    if (!same) {
      colFr = List<double>.filled(cols, 1);
      rowFr = List<double>.filled(rows, 1);
    }
    _applyGridShape();
  }

  /// One slot of web's slotRect (grid.html:493): the tile position for
  /// [index] in a cols×rows canvas of [areaWidth]×[areaHeight].
  static CanvasRect slotRect(int index, int cols, int rows, double areaWidth, double areaHeight) {
    const gap = 8.0;
    final perRow = math.max(1, cols);
    final area = math.max(360.0, areaWidth - 16);
    final cellW = ((area - gap * (perRow - 1) - 16) / perRow).floorToDouble();
    final rowsCount = math.max(1, rows);
    final cellH = math.max(
      canvasMinHeight,
      ((areaHeight - gap * (rowsCount - 1) - 16) / rowsCount).floorToDouble(),
    );
    final col = index % perRow;
    final row = index ~/ perRow;
    return CanvasRect(x: 8 + col * (cellW + gap), y: 8 + row * (cellH + gap), w: cellW, h: cellH);
  }

  /// Web's bestShape (grid.html:507-531): score candidate shapes by
  /// log-ratio against the ~1.4 aspect, +0.30 per hole, +0.35 vertical
  /// penalty (rows > cols), and a narrow-column squeeze penalty, so
  /// the result naturally prefers 2×N/3×N over vertical stacks.
  static ({int cols, int rows}) bestShape(int count, double width, double height) {
    final n = math.max(1, count);
    if (n == 1) return (cols: 1, rows: 1);
    if (width <= 760) return (cols: 1, rows: n);
    const gap = 8.0;
    final w = math.max(420.0, width - 24);
    final h = math.max(320.0, height - 24);
    ({int cols, int rows, double score})? best;
    for (var cols = 1; cols <= n; cols++) {
      final rows = (n / cols).ceil();
      final cw = (w - gap * (cols - 1)) / cols;
      final ch = (h - gap * (rows - 1)) / rows;
      if (cw < canvasMinWidth || ch < canvasMinHeight) continue;
      final ratio = (math.log((cw / ch) / 1.4)).abs();
      final holes = (cols * rows - n) * 0.30;
      final vertical = rows > cols ? 0.35 : 0.0;
      final squash = math.max(0.0, 1 - cw / (canvasMinWidth * 1.35)) * 1.5;
      final score = ratio + holes + vertical + squash;
      if (best == null || score < best.score - 1e-6) best = (cols: cols, rows: rows, score: score);
    }
    if (best == null) {
      final cols = math.min(n, math.max(1, math.sqrt(n).round()));
      return (cols: cols, rows: (n / cols).ceil());
    }
    return (cols: best.cols, rows: best.rows);
  }

  /// Tile every pane into a cols×rows canvas (web's tileCanvas).
  void tileCanvas(int useCols, int useRows, double areaWidth, double areaHeight) {
    final count = math.max(1, panes.length);
    final cols = math.max(1, math.min(useCols, count));
    final rows = math.max(1, useRows);
    cells.clear();
    for (var i = 0; i < panes.length; i++) {
      cells[panes[i]] = slotRect(i, cols, rows, areaWidth, areaHeight);
    }
    canvasCols = cols;
    canvasRows = rows;
  }

  /// Single-level undo snapshot (web's layoutUndo, grid.html:533-551):
  /// canvas cells + canvas shape + pane order, taken before a tidy,
  /// a canvas shape change, or a canvas drag commit.
  Map<String, dynamic> layoutSnapshot() => {
        'cells': {for (final entry in cells.entries) entry.key: entry.value.toJson()},
        'canvasCols': canvasCols,
        'canvasRows': canvasRows,
        'panes': List<String>.from(panes),
      };

  void restoreLayoutSnapshot(Map<String, dynamic> snapshot) {
    cells.clear();
    final rawCells = snapshot['cells'];
    if (rawCells is Map) {
      for (final entry in rawCells.entries) {
        if (entry.value is Map) cells['${entry.key}'] = CanvasRect.fromJson(Map<String, dynamic>.from(entry.value as Map));
      }
    }
    canvasCols = int.tryParse('${snapshot['canvasCols']}') ?? 0;
    canvasRows = int.tryParse('${snapshot['canvasRows']}') ?? 0;
    final savedPanes = snapshot['panes'];
    if (savedPanes is List) {
      panes
        ..clear()
        ..addAll(savedPanes.whereType<String>());
    }
    _applyGridShape();
  }

  /// Snapshot for a named preset, like web's preset object.
  Map<String, dynamic> presetSnapshot() => toJson();

  void applyPreset(Map<String, dynamic> preset) {
    final restored = GridState.fromJson(preset);
    panes
      ..clear()
      ..addAll(restored.panes);
    widths
      ..clear()
      ..addAll(restored.widths);
    layout = restored.layout;
    cols = restored.cols;
    rows = restored.rows;
    colFr = restored.colFr;
    rowFr = restored.rowFr;
    modes
      ..clear()
      ..addAll(restored.modes);
    cells
      ..clear()
      ..addAll(restored.cells);
    canvasCols = restored.canvasCols;
    canvasRows = restored.canvasRows;
    custom = restored.custom;
    extras
      ..clear()
      ..addAll(restored.extras.map((key, value) => MapEntry(key, _deepCopy(value))));
    // A preset's term modes are deliberate choices, like web's apply.
    explicit
      ..clear()
      ..addAll(restored.modes.keys);
    maximized = null;
    _applyGridShape();
  }
}
