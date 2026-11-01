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
      // 出错的 socket 和控制器都没人再管了，关掉免得每次失败漏一个。
      if (!controller.isClosed) controller.close();
    });
    socket.onClose.listen((_) {
      // 握手失败时浏览器只给 close、不给 open：completer 还悬着就让 connect()
      // 以为还在连接，终端永远停在「连接中…」，ScreenStream 的重连也永远不会排上。
      if (!completer.isCompleted) {
        completer.completeError(StateError('WebSocket closed before it opened: $uri'));
      }
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
