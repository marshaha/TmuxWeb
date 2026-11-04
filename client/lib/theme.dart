import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// One palette. Hex values come from web/assets/theme.css (the five named
/// themes) and from web/index.html:18 (the warm default, which has no
/// theme.css block of its own). --surface2, --card-hover, --line-strong
/// and --shimmer are web-exact per theme; dark claude-code card is web
/// --card #2a2725 (not --surface #232120) and dark night card is web
/// --card #24252b (not --surface #1E1F25).
class AppPalette {
  const AppPalette({
    required this.name,
    required this.label,
    required this.hint,
    required this.ink,
    required this.muted,
    required this.faint,
    required this.line,
    required this.page,
    required this.sidebar,
    required this.card,
    required this.surface2,
    required this.cardHover,
    required this.shimmer,
    required this.lineStrong,
    required this.accent,
    required this.green,
    required this.danger,
    required this.isDark,
  });

  final String name;
  final String label;
  final String hint;
  final Color ink;
  final Color muted;
  final Color faint;
  final Color line;
  final Color page;
  final Color sidebar;
  final Color card;

  /// Web --surface2: the secondary surface (inputs, result boxes).
  final Color surface2;

  /// Web --card-hover: hover background for rows and cards.
  final Color cardHover;

  /// Web --shimmer: skeleton shimmer tint.
  final Color shimmer;

  /// Web --line-strong: the heavier border (chips, focused controls).
  final Color lineStrong;
  final Color accent;
  final Color green;
  final Color danger;
  final bool isDark;

  TextStyle get mono => TextStyle(fontFamily: 'monospace', fontSize: 12.5, height: 1.5, color: ink);
}

class AppThemes {
  static const warm = AppPalette(
    name: 'warm',
    label: '默认',
    hint: '暖灰 + 陶土橙',
    ink: Color(0xFF242421),
    muted: Color(0xFF73716B),
    faint: Color(0xFF9B998F),
    line: Color(0xFFDDDCD5),
    page: Color(0xFFF7F7F5),
    sidebar: Color(0xFFEEEDE8),
    card: Color(0xFFFFFFFF),
    surface2: Color(0xFFF4F3EE),
    cardHover: Color(0xFFF3F2ED),
    shimmer: Color(0xFFF3F2ED),
    lineStrong: Color(0xFFCBC9C0),
    accent: Color(0xFFC96442),
    green: Color(0xFF4F9A65),
    danger: Color(0xFFB34B43),
    isDark: false,
  );

  static const chatgpt = AppPalette(
    name: 'chatgpt',
    label: 'ChatGPT',
    hint: '白底 + 绿色强调',
    ink: Color(0xFF0D0D0D),
    muted: Color(0xFF5D5D5D),
    faint: Color(0xFF8E8E8E),
    line: Color(0xFFE3E3E3),
    page: Color(0xFFFFFFFF),
    sidebar: Color(0xFFF7F7F8),
    card: Color(0xFFFFFFFF),
    surface2: Color(0xFFF7F7F8),
    cardHover: Color(0xFFF2F2F2),
    shimmer: Color(0xBFFFFFFF),
    lineStrong: Color(0xFFD9D9D9),
    accent: Color(0xFF10A37F),
    green: Color(0xFF10A37F),
    danger: Color(0xFFEF4146),
    isDark: false,
  );

  static const claude = AppPalette(
    name: 'claude',
    label: 'Claude',
    hint: '米白 + 陶土橙',
    ink: Color(0xFF1F1E1D),
    muted: Color(0xFF6B6A63),
    faint: Color(0xFFA49F95),
    line: Color(0xFFE2DDD2),
    page: Color(0xFFFAF9F5),
    sidebar: Color(0xFFF0EEE6),
    card: Color(0xFFFFFFFF),
    surface2: Color(0xFFF5F3EE),
    cardHover: Color(0xFFF4F2EC),
    shimmer: Color(0xBFFFFFFF),
    lineStrong: Color(0xFFD8D2C6),
    accent: Color(0xFFD97757),
    green: Color(0xFF5A8F6B),
    danger: Color(0xFFB04A3F),
    isDark: false,
  );

  static const github = AppPalette(
    name: 'github',
    label: 'GitHub',
    hint: '白底 + 蓝色强调',
    ink: Color(0xFF1F2328),
    muted: Color(0xFF59636E),
    faint: Color(0xFF818B98),
    line: Color(0xFFD1D9E0),
    page: Color(0xFFFFFFFF),
    sidebar: Color(0xFFF6F8FA),
    card: Color(0xFFFFFFFF),
    surface2: Color(0xFFF6F8FA),
    cardHover: Color(0xFFF0F3F6),
    shimmer: Color(0xBFFFFFFF),
    lineStrong: Color(0xFFC3CCD6),
    accent: Color(0xFF0969DA),
    green: Color(0xFF1A7F37),
    danger: Color(0xFFCF222E),
    isDark: false,
  );

  static const claudeCode = AppPalette(
    name: 'claude-code',
    label: 'Claude Code',
    hint: '深色终端 + 橙色强调',
    ink: Color(0xFFE8E4DC),
    muted: Color(0xFFA8A29A),
    faint: Color(0xFF7C766E),
    line: Color(0xFF3A3632),
    page: Color(0xFF1A1917),
    sidebar: Color(0xFF151412),
    card: Color(0xFF2A2725),
    surface2: Color(0xFF2A2725),
    cardHover: Color(0xFF332F2C),
    shimmer: Color(0x0FFFFFFF),
    lineStrong: Color(0xFF4A453F),
    accent: Color(0xFFD97757),
    green: Color(0xFF5AA87A),
    danger: Color(0xFFE0776B),
    isDark: true,
  );

  static const night = AppPalette(
    name: 'night',
    label: '夜间',
    hint: '中性深灰 + 蓝色强调',
    ink: Color(0xFFE6E6E3),
    muted: Color(0xFFA5A39C),
    faint: Color(0xFF76746E),
    line: Color(0xFF33343D),
    page: Color(0xFF15161A),
    sidebar: Color(0xFF111216),
    card: Color(0xFF24252B),
    surface2: Color(0xFF25262D),
    cardHover: Color(0xFF2C2D35),
    shimmer: Color(0x0FFFFFFF),
    lineStrong: Color(0xFF3F414B),
    accent: Color(0xFF7AA2F7),
    green: Color(0xFF6CC08B),
    danger: Color(0xFFD4736A),
    isDark: true,
  );

  static const all = [warm, chatgpt, claude, github, claudeCode, night];

  static AppPalette byName(String? name) =>
      all.firstWhere((palette) => palette.name == name, orElse: () => warm);
}

/// The live theme. Screens read the static getters at build time, so a
/// theme change repaints a screen when that screen rebuilds; MaterialApp
/// itself rebuilds from [notifier] (see main.dart), which repaints the
/// Theme.of dependents (AppBar, Scaffold, inputs, buttons) across routes.
///
/// Invariant that makes the static getters correct: the theme is only
/// changed from the settings screen, which is only reachable from the
/// hosts screen, and the hosts screen reloads (and so rebuilds) when the
/// settings screen returns. A settings entry added anywhere else must do
/// the same reload-on-return.
class AppTheme {
  static AppPalette _current = AppThemes.warm;
  static final ValueNotifier<AppPalette> notifier = ValueNotifier<AppPalette>(AppThemes.warm);

  static AppPalette get current => _current;
  static String get name => _current.name;

  static void set(AppPalette palette) {
    _current = palette;
    notifier.value = palette;
  }

  static void resetForTest() => set(AppThemes.warm);

  static Color get ink => _current.ink;
  static Color get muted => _current.muted;
  static Color get faint => _current.faint;
  static Color get line => _current.line;
  static Color get page => _current.page;
  static Color get sidebar => _current.sidebar;
  static Color get card => _current.card;
  static Color get surface2 => _current.surface2;
  static Color get cardHover => _current.cardHover;
  static Color get shimmer => _current.shimmer;
  static Color get lineStrong => _current.lineStrong;
  static Color get accent => _current.accent;
  static Color get green => _current.green;
  static Color get danger => _current.danger;
  static TextStyle get mono => _current.mono;

  /// Surfaces the transcript paints itself. On the dark palettes they must lift off
  /// the card instead of staying cream, otherwise near-white ink lands on cream and
  /// the two dark themes are unreadable.
  static Color get bubble =>
      _current.isDark ? Color.alphaBlend(_current.ink.withValues(alpha: 0.10), _current.card) : const Color(0xFFE9E7DF);
  static Color get codeBg =>
      _current.isDark ? Color.alphaBlend(_current.ink.withValues(alpha: 0.07), _current.card) : const Color(0xFFF1EFE7);
  static Color get tileLine => _current.isDark ? _current.line : const Color(0xFFE1DFD7);
  static Color get tileGrid => _current.isDark ? _current.line : const Color(0xFFD1CFC6);

  static ThemeData themeData([AppPalette? palette]) {
    final colors = palette ?? _current;
    final base = colors.isDark ? ThemeData.dark(useMaterial3: true) : ThemeData.light(useMaterial3: true);
    return base.copyWith(
      scaffoldBackgroundColor: colors.page,
      colorScheme: base.colorScheme.copyWith(
        primary: colors.ink,
        secondary: colors.accent,
        surface: colors.card,
        error: colors.danger,
      ),
      appBarTheme: AppBarTheme(
        backgroundColor: colors.page,
        foregroundColor: colors.ink,
        elevation: 0,
        scrolledUnderElevation: 0.5,
        centerTitle: false,
        titleTextStyle: TextStyle(color: colors.ink, fontSize: 16, fontWeight: FontWeight.w600),
      ),
      cardTheme: CardThemeData(
        color: colors.card,
        surfaceTintColor: Colors.transparent,
        shadowColor: Colors.transparent,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(color: colors.line),
        ),
      ),
      dividerColor: colors.line,
      textSelectionTheme: TextSelectionThemeData(
        cursorColor: colors.accent,
        selectionColor: colors.accent.withValues(alpha: 0.2),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: colors.card,
        isDense: true,
        contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: BorderSide(color: colors.line)),
        enabledBorder:
            OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: BorderSide(color: colors.line)),
        focusedBorder:
            OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: BorderSide(color: colors.faint)),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: colors.ink,
          foregroundColor: colors.isDark ? colors.page : Colors.white,
          minimumSize: const Size.fromHeight(48),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
      ),
      snackBarTheme: SnackBarThemeData(behavior: SnackBarBehavior.floating, backgroundColor: colors.ink),
    );
  }

  /// Historical name for the ThemeData builder; kept so existing tests
  /// and main.dart need no rename. It builds from the current palette,
  /// light or dark.
  static ThemeData light() => themeData();

  // -- persistence: the theme follows the account, like web ------------
  // Web keeps the account theme in a cookie plus localStorage fallback.
  // Flutter keeps it in SharedPreferences, per server (an account lives
  // on one server) plus a global last-used copy for the login screen.
  static const _globalKey = 'tmux-theme';
  static String _serverKey(String baseUrl) => 'tmux-theme:$baseUrl';

  static Future<void> restoreCached({String? baseUrl}) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final name = (baseUrl != null ? prefs.getString(_serverKey(baseUrl)) : null) ?? prefs.getString(_globalKey);
      if (name != null && AppThemes.byName(name).name == name) set(AppThemes.byName(name));
    } catch (_) {
      // preferences are a convenience only
    }
  }

  static Future<void> cache(String baseUrl, String name) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_serverKey(baseUrl), name);
      await prefs.setString(_globalKey, name);
    } catch (_) {
      // preferences are a convenience only
    }
  }
}
