import 'package:flutter/material.dart';

import 'screens/login_screen.dart';
import 'theme.dart';

void main() => runApp(const TmuxWebApp());

/// Native client for a self-hosted TmuxWeb: log in with URL + password, then
/// browse panes and talk to the sessions directly over the HTTP API.
class TmuxWebApp extends StatelessWidget {
  const TmuxWebApp({super.key, this.home});

  final Widget? home;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'TmuxWeb',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light(),
      home: home ?? const LoginScreen(),
    );
  }
}
