import 'dart:io';

/// Native WebSocket: full handshake headers, text and binary frames.
class AppSocket {
  AppSocket._(this._socket);

  final WebSocket _socket;

  static Future<AppSocket> connect(Uri uri, {Map<String, String> headers = const {}}) async {
    final socket = await WebSocket.connect(uri.toString(), headers: headers);
    return AppSocket._(socket);
  }

  Stream<dynamic> get stream => _socket;

  bool get isOpen => _socket.readyState == WebSocket.open;

  void add(String data) {
    if (isOpen) _socket.add(data);
  }

  Future<void> close() => _socket.close();
}
