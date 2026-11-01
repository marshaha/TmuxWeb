// Save bytes where the user can find them and report where they
// went: the app-documents tmuxweb/ folder natively, the browser's
// download (named file) on web. Returns the display location used
// by the caller's snackbar or status line.
export 'save_file_io.dart' if (dart.library.js_interop) 'save_file_web.dart';
