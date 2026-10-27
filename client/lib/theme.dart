import 'package:flutter/material.dart';

class AppTheme {
  static const ink = Color(0xFF242421);
  static const muted = Color(0xFF73716B);
  static const faint = Color(0xFF9B998F);
  static const line = Color(0xFFDDDCD5);
  static const page = Color(0xFFF7F6F2);
  static const card = Color(0xFFFFFFFF);
  static const accent = Color(0xFFC96442);
  static const green = Color(0xFF4F9A65);

  static ThemeData light() {
    final base = ThemeData.light(useMaterial3: true);
    return base.copyWith(
      scaffoldBackgroundColor: page,
      colorScheme: base.colorScheme.copyWith(
        primary: ink,
        secondary: accent,
        surface: card,
        error: const Color(0xFFB34B43),
      ),
      appBarTheme: const AppBarTheme(
        backgroundColor: page,
        foregroundColor: ink,
        elevation: 0,
        scrolledUnderElevation: 0.5,
        centerTitle: false,
        titleTextStyle: TextStyle(color: ink, fontSize: 16, fontWeight: FontWeight.w600),
      ),
      dividerColor: line,
      textSelectionTheme: const TextSelectionThemeData(cursorColor: accent, selectionColor: Color(0x33C96442)),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: card,
        isDense: true,
        contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: line)),
        enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: line)),
        focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: faint)),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: ink,
          foregroundColor: Colors.white,
          minimumSize: const Size.fromHeight(48),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
      ),
      snackBarTheme: const SnackBarThemeData(behavior: SnackBarBehavior.floating, backgroundColor: ink),
    );
  }

  static const mono = TextStyle(fontFamily: 'monospace', fontSize: 12.5, height: 1.5, color: ink);
}
