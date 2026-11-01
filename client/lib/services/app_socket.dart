// A WebSocket both native and web builds can use. Native code gets
// dart:io's socket (with handshake headers); the browser build gets
// package:web's socket, where headers cannot be set — the browser
// sends only its own cookies, so socket auth on web is limited to
// whatever the browser already holds for that origin.
export 'app_socket_io.dart' if (dart.library.js_interop) 'app_socket_web.dart';
