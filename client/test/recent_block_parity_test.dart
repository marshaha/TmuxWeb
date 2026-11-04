import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/theme.dart';
import 'package:tmuxweb_client/widgets/recent_block.dart';

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
