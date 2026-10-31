import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/screens/home_board_screen.dart';
import 'package:tmuxweb_client/screens/home_search_screen.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart';

import 'screens_test.dart' show FakeApi, wrap;

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('board renders stats, favorites, recent and cards', (tester) async {
    await tester.pumpWidget(wrap(HomeBoardScreen(api: FakeApi())));
    await tester.pumpAndSettle();

    expect(find.text('2 panes'), findsOneWidget);
    expect(find.text('1 busy'), findsOneWidget);
    expect(find.text('review'), findsOneWidget);
    // The preview is a Text.rich span, so textContaining, not text, matches it.
    expect(find.textContaining('已完成第一版'), findsOneWidget);
    expect(find.text('agent'), findsOneWidget);
    expect(find.text('结论'), findsOneWidget);
    expect(find.text('先做 API 再做界面'), findsOneWidget);
  });

  testWidgets('board favorite opens its chat', (tester) async {
    await tester.pumpWidget(wrap(HomeBoardScreen(api: FakeApi())));
    await tester.pumpAndSettle();

    await tester.tap(find.text('review').first);
    await tester.pumpAndSettle();
    expect(find.text('来自 %7 的回复'), findsOneWidget);
  });

  testWidgets('unfavorite, rename and card delete call the board API', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(HomeBoardScreen(api: api)));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('unfav-%7')));
    await tester.pumpAndSettle();
    expect(api.unfavorited, ['%7']);

    await tester.tap(find.byKey(const Key('rename-%7')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('rename-title')), '新标题');
    await tester.tap(find.text('保存'));
    await tester.pumpAndSettle();
    expect(api.renamedFavorites, ['%7|新标题']);

    await tester.ensureVisible(find.byKey(const Key('delete-card-c1')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('delete-card-c1')));
    await tester.pumpAndSettle();
    expect(api.deletedCards, ['c1']);
  });

  testWidgets('search renders grouped results', (tester) async {
    await tester.pumpWidget(wrap(HomeSearchScreen(api: FakeApi())));
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('search-query')), 'codex');
    await tester.tap(find.byKey(const Key('search-submit')));
    await tester.pumpAndSettle();

    expect(find.text('匹配方式：literal'), findsOneWidget);
    expect(find.text('localhost'), findsWidgets);
    expect(find.text('review'), findsWidgets);
    expect(find.text('已完成第一版'), findsOneWidget);
    // The cards section is below the viewport until scrolled into view.
    await tester.drag(find.byType(ListView).first, const Offset(0, -800));
    await tester.pumpAndSettle();
    expect(find.text('结论'), findsOneWidget);
  });

  testWidgets('hosts screen opens the board', (tester) async {
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://chat.local:8899')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('home-board')));
    await tester.pumpAndSettle();
    expect(find.text('主页板'), findsWidgets);
    expect(find.text('review'), findsOneWidget);
  });
}
