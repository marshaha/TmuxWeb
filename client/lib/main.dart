import 'package:flutter/material.dart';

import 'screens/login_screen.dart';
import 'theme.dart';

void main() => runApp(const TmuxWebApp());

/// Native client for a self-hosted TmuxWeb: log in with URL + password, then
/// browse panes and talk to the sessions directly over the HTTP API.
class TmuxWebApp extends StatefulWidget {
  const TmuxWebApp({super.key, this.home});

  final Widget? home;

  @override
  State<TmuxWebApp> createState() => _TmuxWebAppState();
}

class _TmuxWebAppState extends State<TmuxWebApp> {
  @override
  void initState() {
    super.initState();
    // Paint the last-used theme before first paint; the login screen
    // replaces it with the account's theme once the server is known.
    AppTheme.restoreCached();
  }

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<AppPalette>(
      valueListenable: AppTheme.notifier,
      builder: (_, palette, __) => MaterialApp(
        title: 'TmuxWeb',
        debugShowCheckedModeBanner: false,
        theme: AppTheme.themeData(palette),
        home: widget.home ?? const LoginScreen(),
      ),
    );
  }
}
