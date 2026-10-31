import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// Live screen stream for screen-mode panes, mirroring web/index.html's
/// `/ws/panes/{id}` socket: the server sends `{type: 'capture',
/// content}` frames — the first carries scrollback, later frames are
/// tail snapshots — and the web client replaces the rendered screen
/// on each frame and reconnects 1.5s after a drop. This class owns
/// the socket and the reconnect timer; closing it stops both.
class ScreenStream {
  ScreenStream({required this.uri, required this.headers});

  final Uri uri;
  final Map<String, String> headers;

  final _controller = StreamController<String>();
  WebSocket? _socket;
  Timer? _retry;
  bool _closed = false;

  Stream<String> get stream => _controller.stream;

  void start() {
    _connect();
  }

  Future<void> _connect() async {
    if (_closed) return;
    try {
      final socket = await WebSocket.connect(uri.toString(), headers: headers);
      if (_closed) {
        await socket.close();
        return;
      }
      _socket = socket;
      socket.listen(
        (data) {
          if (_closed || data is! String) return;
          try {
            final decoded = jsonDecode(data);
            if (decoded is Map && decoded['type'] == 'capture' && decoded['content'] is String) {
              _controller.add(decoded['content'] as String);
            }
          } catch (_) {
            // A malformed frame is skipped; the next capture replaces it.
          }
        },
        onDone: _scheduleReconnect,
        onError: (_) => _scheduleReconnect(),
        cancelOnError: true,
      );
    } catch (_) {
      _scheduleReconnect();
    }
  }

  void _scheduleReconnect() {
    if (_closed) return;
    _socket = null;
    _retry?.cancel();
    _retry = Timer(const Duration(milliseconds: 1500), _connect);
  }

  Future<void> close() async {
    _closed = true;
    _retry?.cancel();
    await _socket?.close();
    await _controller.close();
  }
}

/// Strips ANSI escape sequences, like web's stripAnsi, so a capture
/// frame renders as the plain text the Flutter screen view shows.
String stripAnsi(String text) => text
    .replaceAll(RegExp(r'\x1B\[[0-9;?]*[ -/]*[@-~]'), '')
    .replaceAll(RegExp(r'\x1B\][^\x07]*(\x07|\x1B\\)'), '');

/// Caps a capture frame like web's trimCapture: the tail under both a
/// line cap and a byte cap, so one pathological frame cannot flood
/// the view.
String trimCapture(String text, {int maxLines = 600, int maxBytes = 160000}) {
  var result = text;
  final lines = result.split('\n');
  if (lines.length > maxLines) {
    result = '… 只显示最近 $maxLines 行 …\n${lines.sublist(lines.length - maxLines).join('\n')}';
  }
  if (result.length > maxBytes) {
    final tail = result.substring(result.length - maxBytes);
    final cut = tail.indexOf('\n');
    result = cut < 0 ? tail : tail.substring(cut + 1);
  }
  return result;
}
