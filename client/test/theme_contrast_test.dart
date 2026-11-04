import 'dart:io';

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

  test('every theme defines all surface variables (web theme.css parity)', () {
    for (final palette in AppThemes.all) {
      // Non-nullable by construction; the pin is that they are real,
      // opaque palette colours, not placeholders carried over from card.
      for (final surface in [palette.surface2, palette.cardHover, palette.lineStrong]) {
        expect((surface.a * 255).round(), 0xFF, reason: '${palette.name} surface must be opaque');
      }
      // Card backgrounds come from the palette, not Material defaults.
      final theme = AppTheme.themeData(palette);
      expect(theme.cardTheme.color, palette.card, reason: '${palette.name} cardTheme');
    }
  });

  test('web-exact surface values, including the dark card corrections', () {
    expect(AppThemes.warm.page, const Color(0xFFF7F7F5));
    // Dark claude-code card is web --card, not --surface.
    expect(AppThemes.claudeCode.card, const Color(0xFF2A2725));
    expect(AppThemes.claudeCode.surface2, const Color(0xFF2A2725));
    expect(AppThemes.claudeCode.cardHover, const Color(0xFF332F2C));
    expect(AppThemes.claudeCode.lineStrong, const Color(0xFF4A453F));
    // Dark night card is web --card, not --surface.
    expect(AppThemes.night.card, const Color(0xFF24252B));
    expect(AppThemes.night.surface2, const Color(0xFF25262D));
    expect(AppThemes.night.cardHover, const Color(0xFF2C2D35));
    expect(AppThemes.night.lineStrong, const Color(0xFF3F414B));
  });

  test('dark card brightness stays under the cap', () {
    for (final palette in AppThemes.all.where((palette) => palette.isDark)) {
      expect(luminance(palette.card), lessThan(0.25), reason: '${palette.name} card');
      expect(luminance(palette.surface2), lessThan(0.25), reason: '${palette.name} surface2');
    }
  });

  test('no hardcoded bright backgrounds in the owned screens and widgets', () {
    // Web test_theme_contrast.py equivalent, scoped to this batch's
    // files (theme.dart itself is the palette definition, so it is the
    // one file exempt from the scan). Bright means a near-white/cream
    // literal that would stay bright under a dark theme.
    const owned = [
      'lib/screens/hosts_screen.dart',
      'lib/screens/home_board_screen.dart',
      'lib/screens/settings_screen.dart',
      'lib/widgets/pane_hover_preview.dart',
      'lib/widgets/recent_block.dart',
    ];
    final hex = RegExp(r'Color\(0xFF([0-9A-Fa-f]{6})\)');
    final rgb = RegExp(r'Color\.fromRGBO\((\d+),\s*(\d+),\s*(\d+),');
    final offenders = <String>[];
    for (final path in owned) {
      final lines = File(path).readAsLinesSync();
      for (var i = 0; i < lines.length; i++) {
        final line = lines[i];
        for (final match in hex.allMatches(line)) {
          // Exempt: web console parity (A6) mandates #cfccc4 for the
          // 6px unknown-state dot — a foreground dot, not a
          // background, so the dark-theme concern does not apply.
          if (match[1]!.toUpperCase() == 'CFCCC4') continue;
          final color = Color(int.parse('FF${match[1]}', radix: 16));
          if (luminance(color) > 0.6) offenders.add('$path:${i + 1}: ${match[0]}');
        }
        for (final match in rgb.allMatches(line)) {
          final color = Color.fromRGBO(
              int.parse(match[1]!), int.parse(match[2]!), int.parse(match[3]!), 1);
          if (luminance(color) > 0.6) offenders.add('$path:${i + 1}: ${match[0]}');
        }
      }
    }
    expect(offenders, isEmpty, reason: 'hardcoded bright backgrounds: ${offenders.join(', ')}');
  });
}
