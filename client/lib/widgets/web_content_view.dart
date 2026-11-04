/// Platform web content view: native WebView on android/ios/macos, a
/// WebView2 view on Windows, an iframe on the Flutter web target. The
/// conditional export mirrors the existing `services/app_socket.dart` /
/// `services/save_file.dart` pattern (`dart.library.js_interop` selects
/// the web implementation).
///
/// Dart conditional imports have no Windows condition, so every non-web
/// target resolves to `web_content_view_windows.dart`, which
/// runtime-dispatches: Windows gets WebView2, all other native targets
/// delegate back to the io implementation unchanged.
///
/// All implementations export the same [WebContentView] widget, so callers
/// (mermaid rendering, plugin pages) never branch on the platform
/// themselves. Tests never instantiate this widget: platform views cannot
/// run under `flutter test`, so callers gate it behind a constructor flag
/// and tests exercise the pure builders and toggle logic instead.
///
/// Auth: a WebView does NOT inherit the Dart HTTP client's cookie jar, so
/// native callers pass the session cookies explicitly (parsed from the
/// `Cookie` header the API client already sends, see [parseCookieHeader])
/// and the native implementations inject them BEFORE loading (io via
/// WebViewCookieManager, Windows via `document.cookie` plus a reload —
/// see `web_content_view_windows.dart`). On the web target the iframe is
/// same-origin, so the browser's own cookies apply automatically and
/// [cookies] is ignored.
library;

export 'web_content_view_windows.dart' if (dart.library.js_interop) 'web_content_view_web.dart';

/// Parses a `Cookie` request header (`name=value; name2=value2`) into a
/// map. Pairs without `=` are skipped; values keep everything after the
/// first `=` (cookie values may themselves contain `=`).
Map<String, String> parseCookieHeader(String header) {
  final cookies = <String, String>{};
  for (final part in header.split(';')) {
    final pair = part.trim();
    if (pair.isEmpty) continue;
    final index = pair.indexOf('=');
    if (index <= 0) continue;
    cookies[pair.substring(0, index).trim()] = pair.substring(index + 1).trim();
  }
  return cookies;
}
