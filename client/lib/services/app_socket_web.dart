import 'dart:async';
import 'dart:js_interop';
import 'dart:typed_data';

import 'package:web/web.dart' as web;

/// Browser WebSocket: same shape as the native one, minus handshake
/// headers (the browser API has no way to set them) and minus
/// server-initiated binary frames beyond arraybuffers.
class AppSocket {
  AppSocket._(this._socket, this._controller);

  final web.WebSocket _socket;
  final StreamController<dynamic> _controller;

  static Future<AppSocket> connect(Uri uri, {Map<String, String> headers = const {}}) {
    final completer = Completer<AppSocket>();
    final socket = web.WebSocket(uri.toString());
    socket.binaryType = 'arraybuffer';
    final controller = StreamController<dynamic>();
    final appSocket = AppSocket._(socket, controller);
    socket.onOpen.listen((_) {
      if (!completer.isCompleted) completer.complete(appSocket);
    });
    socket.onMessage.listen((event) {
      // dartify() turns a JS string into a Dart String and an
      // ArrayBuffer into a ByteBuffer; anything else (Blob frames)
      // is ignored — the server sends text frames for both the
      // terminal and screen streams.
      final data = event.data?.dartify();
      if (data is String) {
        controller.add(data);
      } else if (data is ByteBuffer) {
        controller.add(data.asUint8List());
      } else if (data is Uint8List) {
        controller.add(data);
      }
    });
    socket.onError.listen((_) {
      if (!completer.isCompleted) {
        completer.completeError(StateError('WebSocket failed: $uri'));
      } else {
        controller.addError(StateError('WebSocket error'));
      }
    });
    socket.onClose.listen((_) {
      if (!controller.isClosed) controller.close();
    });
    return completer.future;
  }

  Stream<dynamic> get stream => _controller.stream;

  /// web.WebSocket.OPEN.
  bool get isOpen => _socket.readyState == 1;

  void add(String data) {
    if (isOpen) _socket.send(data.toJS);
  }

  Future<void> close() async {
    _socket.close();
    if (!_controller.isClosed) await _controller.close();
  }
}
