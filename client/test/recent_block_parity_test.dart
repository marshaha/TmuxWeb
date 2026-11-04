import 'package:flutter/material.dart';
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
  testWidgets('active row gets the accent tint, inactive does not', (tester) async {
    Widget row(bool active) => MaterialApp(
          home: Scaffold(
            body: RecentRow(
              data: RecentRowData(paneRef: '%7', label: 'review', active: active),
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
    expect(tester.widget<Container>(find.byKey(const Key('recent-row-%7'))).decoration, isNull);
  });

  // E14: web suppresses the context menu on pinned rows (recents.js:517-519).
  testWidgets('pinned rows claim secondary tap, unpinned rows do not', (tester) async {
    Widget row(bool pinned) => MaterialApp(
          home: Scaffold(
            body: RecentRow(
              data: RecentRowData(paneRef: '%7', label: 'review', pinned: pinned),
              onOpen: (_) {},
              onTogglePin: (_, __) async {},
              onRename: (_, __) async {},
              onToggleFavorite: (_, __) async {},
            ),
          ),
        );
    await tester.pumpWidget(row(true));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('recent-row-context-suppress-%7')), findsOneWidget);
    await tester.pumpWidget(row(false));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('recent-row-context-suppress-%7')), findsNothing);
  });
}

