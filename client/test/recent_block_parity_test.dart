import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/theme.dart';
import 'package:tmuxweb_client/widgets/pane_hover_preview.dart';
import 'package:tmuxweb_client/widgets/recent_block.dart';

int _pumpSeq = 0;

/// Pumps a RecentBlock with canned data. Callbacks record into the
/// lists passed in; every parameter defaults to the block's own default
/// so each test only passes what it exercises. Each call gets a fresh
/// widget key so a re-pump starts new state (otherwise the 15s reload
/// dedup would keep the previous pump's rows).
Future<void> _pumpBlock(
  WidgetTester tester, {
  RecentBlockData data = const RecentBlockData(),
  void Function(String paneRef)? onOpen,
  Future<void> Function(String paneRef, bool pinned)? onTogglePin,
  Future<void> Function(String paneRef, String label)? onRenamePin,
  Future<void> Function(String paneRef, bool starred)? onToggleFavorite,
  Future<void> Function(List<String> paneRefs)? onClearUnpinned,
  Future<void> Function(String paneRef, String label)? onRenameFavoriteTitle,
  String? activePaneRef,
  bool hideWhenFiles = false,
  bool isFilesPanelOpen = false,
  Set<String>? knownPaneRefs,
  double? sidebarHeight,
  bool fixedBottom = true,
  bool fillHeight = false,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: RecentBlock(
          key: ValueKey('recent-block-pump-${++_pumpSeq}'),
          load: () async => data,
          onOpen: onOpen ?? (_) {},
          onTogglePin: onTogglePin ?? (_, __) async {},
          onRenamePin: onRenamePin ?? (_, __) async {},
          onToggleFavorite: onToggleFavorite ?? (_, __) async {},
          onClearUnpinned: onClearUnpinned,
          onRenameFavoriteTitle: onRenameFavoriteTitle,
          activePaneRef: activePaneRef,
          hideWhenFiles: hideWhenFiles,
          isFilesPanelOpen: isFilesPanelOpen,
          knownPaneRefs: knownPaneRefs,
          sidebarHeight: sidebarHeight,
          fixedBottom: fixedBottom,
          fillHeight: fillHeight,
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

RecentRowData _row(String ref,
        {bool pinned = false, bool starred = false, String status = 'idle'}) =>
    RecentRowData(
        paneRef: ref,
        label: 'label-$ref',
        pinned: pinned,
        starred: starred,
        status: status);

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    AppTheme.resetForTest();
  });
  tearDown(AppTheme.resetForTest);

  // E6: web relativeTime (recents.js:127-135), no suffix, '' at/after now.
  group('formatRecentVisited (E6)', () {
    test('relative buckets match web', () {
      final now = DateTime(2026, 11, 4, 12, 0, 0);
      double ago(int seconds) => now.millisecondsSinceEpoch / 1000 - seconds;
      expect(formatRecentVisited(ago(0), now: now), '');
      expect(formatRecentVisited(ago(30), now: now), '刚刚');
      expect(formatRecentVisited(ago(90), now: now), '1 分钟');
      expect(formatRecentVisited(ago(5 * 3600), now: now), '5 小时');
      expect(formatRecentVisited(ago(3 * 86400), now: now), '3 天');
      expect(formatRecentVisited(0, now: now), '');
      expect(formatRecentVisited(ago(-10), now: now), '');
    });
  });

  // E5: web .active row (recents.js markActive, recents.css .active).
  testWidgets('active row gets the accent tint, inactive does not',
      (tester) async {
    Widget row(bool active) => MaterialApp(
          home: Scaffold(
            body: RecentRow(
              data:
                  RecentRowData(paneRef: '%7', label: 'review', active: active),
              onOpen: (_) {},
              onTogglePin: (_, __) async {},
              onRename: (_, __) async {},
              onToggleFavorite: (_, __) async {},
            ),
          ),
        );
    await tester.pumpWidget(row(true));
    await tester.pumpAndSettle();
    final activeBox =
        tester.widget<Container>(find.byKey(const Key('recent-row-%7')));
    expect((activeBox.decoration as BoxDecoration?)?.color, isNotNull);
    await tester.pumpWidget(row(false));
    await tester.pumpAndSettle();
    expect(
        tester
            .widget<Container>(find.byKey(const Key('recent-row-%7')))
            .decoration,
        isNull);
  });

  // E14: web suppresses the context menu on pinned rows (recents.js:517-519).
  testWidgets('pinned rows claim secondary tap, unpinned rows do not',
      (tester) async {
    Widget row(bool pinned) => MaterialApp(
          home: Scaffold(
            body: RecentRow(
              data:
                  RecentRowData(paneRef: '%7', label: 'review', pinned: pinned),
              onOpen: (_) {},
              onTogglePin: (_, __) async {},
              onRename: (_, __) async {},
              onToggleFavorite: (_, __) async {},
            ),
          ),
        );
    await tester.pumpWidget(row(true));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('recent-row-context-suppress-%7')),
        findsOneWidget);
    await tester.pumpWidget(row(false));
    await tester.pumpAndSettle();
    expect(
        find.byKey(const Key('recent-row-context-suppress-%7')), findsNothing);
  });

  // E10: web rename semantics (recents.js:349-390).
  group('rename (E10)', () {
    late List<String> renamed;
    Widget row(
        {String pinLabel = '我的名字',
        String label = 'review',
        bool pinned = true,
        String ref = '%7'}) {
      renamed = [];
      return MaterialApp(
        home: Scaffold(
          body: Column(
            children: [
              RecentRow(
                key: ValueKey('row-$ref'),
                data: RecentRowData(
                    paneRef: ref,
                    label: label,
                    pinLabel: pinLabel,
                    pinned: pinned),
                onOpen: (_) {},
                onTogglePin: (_, __) async {},
                onRename: (_, value) async {
                  renamed.add(value);
                },
                onToggleFavorite: (_, __) async {},
              ),
              TextButton(onPressed: () {}, child: const Text('elsewhere')),
            ],
          ),
        ),
      );
    }

    Future<void> openViaPencil(WidgetTester tester) async {
      await tester.tap(find.byKey(const Key('recent-rename-%7')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('recent-rename-input-%7')), findsOneWidget);
    }

    testWidgets('pencil entry starts with pin label, Enter saves trimmed',
        (tester) async {
      await tester.pumpWidget(row());
      await openViaPencil(tester);
      expect(
          tester
              .widget<TextField>(
                  find.byKey(const Key('recent-rename-input-%7')))
              .controller
              ?.text,
          '我的名字');
      await tester.enterText(
          find.byKey(const Key('recent-rename-input-%7')), '  新名字  ');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(renamed, ['新名字']);
      expect(find.byKey(const Key('recent-rename-input-%7')), findsNothing);
    });

    testWidgets('initial text falls back to the displayed label',
        (tester) async {
      await tester.pumpWidget(row(pinLabel: ''));
      await openViaPencil(tester);
      expect(
          tester
              .widget<TextField>(
                  find.byKey(const Key('recent-rename-input-%7')))
              .controller
              ?.text,
          'review');
    });

    testWidgets('Escape cancels without saving', (tester) async {
      await tester.pumpWidget(row());
      await openViaPencil(tester);
      await tester.enterText(
          find.byKey(const Key('recent-rename-input-%7')), '丢弃');
      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await tester.pumpAndSettle();
      expect(renamed, isEmpty);
      expect(find.byKey(const Key('recent-rename-input-%7')), findsNothing);
    });

    testWidgets('blur saves', (tester) async {
      await tester.pumpWidget(row());
      await openViaPencil(tester);
      await tester.enterText(
          find.byKey(const Key('recent-rename-input-%7')), '失焦保存');
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pumpAndSettle();
      expect(renamed, ['失焦保存']);
    });

    testWidgets('touch long-press enters rename on pinned rows',
        (tester) async {
      await tester.pumpWidget(row());
      await tester.longPress(find.byKey(const Key('recent-row-%7')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('recent-rename-input-%7')), findsOneWidget);
    });

    // ---- RecentBlock items ----
    group('RecentBlock header and hiding (E2/E3/E4)', () {
      testWidgets('E2: header is just 最近, no count', (tester) async {
        await _pumpBlock(tester,
            data: RecentBlockData(
                pinned: [_row('%7', pinned: true)], recent: [_row('%8')]));
        expect(find.text('最近'), findsOneWidget);
        expect(find.text('最近 2'), findsNothing);
      });

      testWidgets('E4: empty rows hide the whole block including the header',
          (tester) async {
        await _pumpBlock(tester, data: const RecentBlockData());
        expect(find.text('最近'), findsNothing);
        expect(find.byKey(const Key('recent-list')), findsNothing);
      });

      testWidgets('E3: console hide-when-files needs both flags',
          (tester) async {
        final data = RecentBlockData(recent: [_row('%8')]);
        await _pumpBlock(tester,
            data: data, hideWhenFiles: true, isFilesPanelOpen: true);
        expect(find.text('最近'), findsNothing);
        await _pumpBlock(tester,
            data: data, hideWhenFiles: true, isFilesPanelOpen: false);
        expect(find.text('最近'), findsOneWidget);
        await _pumpBlock(tester,
            data: data, hideWhenFiles: false, isFilesPanelOpen: true);
        expect(find.text('最近'), findsOneWidget);
      });
    });

    group('RecentBlock filtering and cap (E7/E8)', () {
      testWidgets('E7: unpinned rows cap at 8, pinned uncapped',
          (tester) async {
        // Tall surface + tall sidebar so the lazy list builds every row.
        tester.view.physicalSize = const Size(800, 2000);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        await _pumpBlock(tester,
            data: RecentBlockData(
              pinned: [_row('%p0', pinned: true), _row('%p1', pinned: true)],
              recent: [for (var i = 0; i < 10; i++) _row('%r$i')],
            ),
            sidebarHeight: 2000);
        for (var i = 0; i < 8; i++) {
          expect(find.text('label-%r$i'), findsOneWidget);
        }
        expect(find.text('label-%r8'), findsNothing);
        expect(find.text('label-%r9'), findsNothing);
        expect(find.text('label-%p0'), findsOneWidget);
        expect(find.text('label-%p1'), findsOneWidget);
      });

      testWidgets(
          'E8: unpinned vanished rows drop, pinned vanished stay offline',
          (tester) async {
        await _pumpBlock(tester,
            data: RecentBlockData(
              pinned: [_row('%gone', pinned: true, status: 'idle')],
              recent: [
                _row('%vanished', status: 'idle'),
                _row('%kept', status: 'idle')
              ],
            ),
            knownPaneRefs: {'%kept'});
        expect(find.text('label-%vanished'), findsNothing);
        expect(find.text('label-%kept'), findsOneWidget);
        expect(find.text('label-%gone'), findsOneWidget);
        final offlineDot = find.byWidgetPredicate((widget) =>
            widget is Container &&
            widget.constraints?.minWidth == 6 &&
            widget.constraints?.maxWidth == 6 &&
            (widget.decoration as BoxDecoration?)?.color == AppTheme.danger);
        expect(offlineDot, findsOneWidget);
      });
    });

    group('RecentBlock header buttons (E1)', () {
      testWidgets(
          'pin-selected pins/unpins the active pane, disabled without one',
          (tester) async {
        final calls = <String>[];
        await _pumpBlock(tester,
            data: RecentBlockData(recent: [_row('%7')]),
            activePaneRef: '%7', onTogglePin: (ref, pinned) async {
          calls.add('$ref:$pinned');
        });
        await tester.tap(find.byKey(const Key('recent-pin-selected')));
        await tester.pumpAndSettle();
        expect(calls, ['%7:true']);

        calls.clear();
        await _pumpBlock(tester,
            data: RecentBlockData(pinned: [_row('%7', pinned: true)]),
            activePaneRef: '%7', onTogglePin: (ref, pinned) async {
          calls.add('$ref:$pinned');
        });
        await tester.tap(find.byKey(const Key('recent-pin-selected')));
        await tester.pumpAndSettle();
        expect(calls, ['%7:false']);

        await _pumpBlock(tester, data: RecentBlockData(recent: [_row('%7')]));
        expect(
            tester
                .widget<IconButton>(
                    find.byKey(const Key('recent-pin-selected')))
                .onPressed,
            isNull);
      });

      testWidgets('clear deletes only unpinned rows', (tester) async {
        var cleared = <String>[];
        await _pumpBlock(tester,
            data: RecentBlockData(
              pinned: [_row('%p', pinned: true)],
              recent: [_row('%a'), _row('%b')],
            ), onClearUnpinned: (refs) async {
          cleared = refs;
        });
        await tester.tap(find.byKey(const Key('recent-clear')));
        await tester.pumpAndSettle();
        expect(cleared, ['%a', '%b']);

        await _pumpBlock(tester, data: RecentBlockData(recent: [_row('%a')]));
        expect(
            tester
                .widget<IconButton>(find.byKey(const Key('recent-clear')))
                .onPressed,
            isNull);
      });
    });

    group('RecentBlock rename favorite sync (E10 block part)', () {
      testWidgets('renaming a starred pin also updates its favorite title',
          (tester) async {
        final pins = <String>[];
        final favs = <String>[];
        await _pumpBlock(tester,
            data: RecentBlockData(
                pinned: [_row('%7', pinned: true, starred: true)]),
            onRenamePin: (ref, label) async {
          pins.add('$ref:$label');
        }, onRenameFavoriteTitle: (ref, label) async {
          favs.add('$ref:$label');
        });
        await tester.tap(find.byKey(const Key('recent-rename-%7')));
        await tester.pumpAndSettle();
        await tester.enterText(
            find.byKey(const Key('recent-rename-input-%7')), '新名');
        await tester.testTextInput.receiveAction(TextInputAction.done);
        await tester.pumpAndSettle();
        expect(pins, ['%7:新名']);
        expect(favs, ['%7:新名']);
      });

      testWidgets('renaming an unstarred pin does not touch favorites',
          (tester) async {
        var favCalls = 0;
        await _pumpBlock(tester,
            data: RecentBlockData(pinned: [_row('%7', pinned: true)]),
            onRenameFavoriteTitle: (_, __) async {
          favCalls++;
        });
        await tester.tap(find.byKey(const Key('recent-rename-%7')));
        await tester.pumpAndSettle();
        await tester.enterText(
            find.byKey(const Key('recent-rename-input-%7')), '新名');
        await tester.testTextInput.receiveAction(TextInputAction.done);
        await tester.pumpAndSettle();
        expect(favCalls, 0);
      });
    });

    group('RecentBlock ratio height and fold key (E11/E12/E13)', () {
      testWidgets(
          'E11: height is ratio x sidebar height, drag persists, dblclick resets',
          (tester) async {
        await _pumpBlock(tester,
            data: RecentBlockData(recent: [_row('%7')]), sidebarHeight: 800);
        expect(
            tester.getSize(find.byKey(const Key('recent-block'))).height, 320);
        await tester.drag(
            find.byKey(const Key('recent-grip')), const Offset(0, -80));
        await tester.pumpAndSettle();
        // The drag recognizer's touch slop eats part of the gesture, so
        // assert growth + persisted-ratio consistency, not exact pixels.
        final draggedHeight =
            tester.getSize(find.byKey(const Key('recent-block'))).height;
        expect(draggedHeight, greaterThan(320));
        final prefs = await SharedPreferences.getInstance();
        expect(
            prefs.getDouble('tmux-recent-h'), closeTo(draggedHeight / 800, 0.001));
        await tester.tap(find.byKey(const Key('recent-grip')));
        await tester.pump(const Duration(milliseconds: 50));
        await tester.tap(find.byKey(const Key('recent-grip')));
        await tester.pumpAndSettle();
        expect(
            tester.getSize(find.byKey(const Key('recent-block'))).height, 320);
        expect(prefs.getDouble('tmux-recent-h'), isNull);
      });

      testWidgets('E11/E12: legacy px height and collapsed keys migrate once',
          (tester) async {
        SharedPreferences.setMockInitialValues({
          'tmux-recent-height': 300.0,
          'tmux-recent-collapsed': true,
        });
        await _pumpBlock(tester,
            data: RecentBlockData(recent: [_row('%7')]), sidebarHeight: 600);
        // Collapsed from the legacy key: header only, no rows.
        expect(find.text('最近'), findsOneWidget);
        expect(find.text('label-%7'), findsNothing);
        final prefs = await SharedPreferences.getInstance();
        expect(prefs.getBool('tmux-recent-fold'), isTrue);
        expect(prefs.getDouble('tmux-recent-h'), 0.5);
        // Expanding writes the new fold key, never the old one.
        await tester.tap(find.byKey(const Key('recent-fold')));
        await tester.pumpAndSettle();
        expect(prefs.getBool('tmux-recent-fold'), isFalse);
        expect(find.text('label-%7'), findsOneWidget);
      });

      testWidgets('E13: fixedBottom mode has the grip, plain mode does not',
          (tester) async {
        await _pumpBlock(tester,
            data: RecentBlockData(recent: [_row('%7')]), fixedBottom: true);
        expect(find.byKey(const Key('recent-grip')), findsOneWidget);
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: Column(
                children: [
                  Expanded(
                    child: RecentBlock(
                      fixedBottom: false,
                      fillHeight: true,
                      load: () async => RecentBlockData(recent: [_row('%7')]),
                      onOpen: (_) {},
                      onTogglePin: (_, __) async {},
                      onRenamePin: (_, __) async {},
                      onToggleFavorite: (_, __) async {},
                    ),
                  ),
                ],
              ),
            ),
          ),
        );
        await tester.pumpAndSettle();
        expect(find.byKey(const Key('recent-grip')), findsNothing);
        expect(find.text('label-%7'), findsOneWidget);
      });
    });

    group('RecentBlock hover preview (M1)', () {
      testWidgets(
          'rows are wrapped in PaneHoverPreview only when loadPreview is given',
          (tester) async {
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: RecentBlock(
                load: () async => RecentBlockData(recent: [_row('%7')]),
                onOpen: (_) {},
                onTogglePin: (_, __) async {},
                onRenamePin: (_, __) async {},
                onToggleFavorite: (_, __) async {},
                loadPreview: (_) async => null,
              ),
            ),
          ),
        );
        await tester.pumpAndSettle();
        expect(find.byType(PaneHoverPreview), findsOneWidget);
        await _pumpBlock(tester, data: RecentBlockData(recent: [_row('%7')]));
        expect(find.byType(PaneHoverPreview), findsNothing);
      });
    });

    testWidgets(
        'double-tap enters rename on pinned rows only, maxlength 80, no save/cancel',
        (tester) async {
      await tester.pumpWidget(row());
      // Double-tap: tester.tap twice in quick succession.
      await tester.tap(find.byKey(const Key('recent-row-%7')));
      await tester.pump(const Duration(milliseconds: 50));
      await tester.tap(find.byKey(const Key('recent-row-%7')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('recent-rename-input-%7')), findsOneWidget);
      expect(
          tester
              .widget<TextField>(
                  find.byKey(const Key('recent-rename-input-%7')))
              .maxLength,
          80);
      expect(find.byKey(const Key('recent-rename-save-%7')), findsNothing);
      expect(find.byKey(const Key('recent-rename-cancel-%7')), findsNothing);

      // A fresh (unpinned) row: double-tap must not enter rename.
      await tester.pumpWidget(row(pinned: false, ref: '%8'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('recent-row-%8')));
      await tester.pump(const Duration(milliseconds: 50));
      await tester.tap(find.byKey(const Key('recent-row-%8')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('recent-rename-input-%8')), findsNothing);
    });
  });
}
