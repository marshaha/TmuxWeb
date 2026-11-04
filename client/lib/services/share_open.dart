// Opens a share URL in a new browser tab on the web target; a no-op on
// native targets (there are no tabs there — SharesScreen falls back to
// its in-app view, its branch already requires kIsWeb). Mirrors the
// app_socket conditional-import pattern.
export 'share_open_stub.dart' if (dart.library.js_interop) 'share_open_web.dart';
