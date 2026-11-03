import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/theme.dart';
import 'package:tmuxweb_client/widgets/message_tile.dart';

/// The dark palettes are the easiest thing to break: half the transcript used to
/// hardcode cream surfaces while the ink came from the palette, so near-white text
/// landed on cream and the message became invisible. These tests pin the derived
/// surfaces to the palette instead of literals.
void main() {
  tearDown(AppTheme.resetForTest);

  double luminance(Color color) => color.computeLuminance();

  test('the transcript surfaces are derived from the palette', () {
    AppTheme.set(AppThemes.warm);
    final warmBubble = AppTheme.bubble;
    final warmCode = AppTheme.codeBg;

    AppTheme.set(AppThemes.claudeCode);
    final darkBubble = AppTheme.bubble;
    final darkCode = AppTheme.codeBg;

    expect(darkBubble, isNot(warmBubble));
    expect(darkCode, isNot(warmCode));
    // 深色主题下气泡必须比白纸暗得多，文字才能读得出来。
    expect(luminance(darkBubble), lessThan(0.25));
    expect(luminance(darkCode), lessThan(0.25));
    expect(luminance(warmBubble), greaterThan(0.6));
  });

  testWidgets('a user bubble paints the palette colour in a dark theme', (tester) async {
    AppTheme.set(AppThemes.night);
    await tester.pumpWidget(
      MaterialApp(
        theme: AppTheme.themeData(),
        home: Scaffold(
          body: MessageTile(message: ChatMessage(id: 'm', role: 'user', text: '你好')),
        ),
      ),
    );
    final container = tester.widget<Container>(find.byType(Container).first);
    final decoration = container.decoration as BoxDecoration;
    expect(decoration.color, AppTheme.bubble);
    // 文字与气泡的亮度要差得开，否则就是"看不见"的那种回归。
    final ink = AppTheme.ink;
    expect((luminance(ink) - luminance(AppTheme.bubble)).abs(), greaterThan(0.35));
  });

  test('markdown styling is rebuilt per access, so a theme switch reaches old messages', () {
    AppTheme.set(AppThemes.warm);
    final before = AppTheme.ink;
    AppTheme.set(AppThemes.claudeCode);
    expect(AppTheme.ink, isNot(before));
  });
}
