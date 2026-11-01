/// Multi-window grid state, mirroring web/grid.html's localStorage
/// model so the two surfaces read and write the same shape:
/// `tmux-grid-v1` for the arrangement, `tmux-grid-presets-v1` for
/// named snapshots. The settings export/import already moves these
/// keys verbatim, so an imported grid lands here without conversion.
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
  })  : panes = panes ?? [],
        widths = widths ?? {},
        colFr = colFr ?? [],
        rowFr = rowFr ?? [],
        modes = modes ?? {},
        explicit = explicit ?? {} {
    _applyGridShape();
  }

  static const gridKey = 'tmux-grid-v1';
  static const presetsKey = 'tmux-grid-presets-v1';

  /// Strip-mode minimum column width, like web's MIN.
  static const minStripWidth = 260.0;

  /// Grid size presets offered in the toolbar, like web's GRID_PRESETS.
  static const gridSizes = [
    (2, 1),
    (2, 2),
    (3, 2),
    (2, 3),
    (3, 3),
  ];

  final List<String> panes;
  final Map<String, double> widths;
  String layout;
  int cols;
  int rows;
  List<double> colFr;
  List<double> rowFr;

  /// Only 'term' entries are stored; chat is the absence of an entry,
  /// exactly like web's modes map.
  final Map<String, String> modes;

  /// Panes whose mode was chosen by hand and must survive the
  /// transcript-based defaulting when re-added.
  final Set<String> explicit;

  /// Transient: never persisted, like web's max.
  String? maximized;

  bool get isGrid => layout == 'grid';

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
    } else if (layout != 'grid') {
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
    );
    return state;
  }

  static List<double> _frList(dynamic raw) {
    if (raw is! List) return [];
    return raw.map((value) => double.tryParse('$value') ?? 1).toList();
  }

  Map<String, dynamic> toJson() => {
        'panes': List<String>.from(panes),
        'widths': Map<String, double>.from(widths),
        'layout': layout,
        'cols': cols,
        'rows': rows,
        'colFr': List<double>.from(colFr),
        'rowFr': List<double>.from(rowFr),
        'modes': Map<String, String>.from(modes),
        'explicit': {for (final pane in explicit) pane: true},
      };

  void _applyGridShape() {
    cols = cols.clamp(1, 4);
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

  void setMode(String pane, String mode) {
    if (mode == 'term') {
      modes[pane] = 'term';
    } else {
      modes.remove(pane);
    }
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
      changed = true;
    }
    if (changed) _applyGridShape();
  }

  void setLayout(String next) {
    layout = next == 'grid' ? 'grid' : 'strip';
    _applyGridShape();
  }

  void setGridSize(int nextCols, int nextRows) {
    layout = 'grid';
    cols = nextCols;
    rows = nextRows;
    colFr = List<double>.filled(cols, 1);
    rowFr = List<double>.filled(rows, 1);
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
    // A preset's term modes are deliberate choices, like web's apply.
    explicit
      ..clear()
      ..addAll(restored.modes.keys);
    maximized = null;
    _applyGridShape();
  }
}
