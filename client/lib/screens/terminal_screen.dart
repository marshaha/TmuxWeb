import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:xterm/xterm.dart';

import '../api/api_client.dart';
import '../models/models.dart';
import '../services/app_socket.dart';
import '../theme.dart';

/// Real PTY terminal for one pane (local or SSH), driven by the server's
/// `/ws/terminal/{pane}` socket.
class TerminalScreen extends StatefulWidget {
  const TerminalScreen({super.key, required this.api, required this.pane, this.embedded = false});

  final TerminalApi api;
  final Pane pane;

  /// Inside a grid cell there is no room for the page chrome: only
  /// the terminal view, like web's /term?embed=1 iframe.
  final bool embedded;

  @override
  State<TerminalScreen> createState() => _TerminalScreenState();
}

class _TerminalScreenState extends State<TerminalScreen> {
  late final Terminal _terminal = Terminal(maxLines: 20000)
    ..onOutput = _sendOutput
    ..onResize = _sendResize;

  AppSocket? _socket;
  String _status = '连接中…';
  bool _alive = true;
  bool _paused = false;
  int _attempt = 0;
  Timer? _reconnectTimer;

  @override
  void initState() {
    super.initState();
    _connect();
  }

  @override
  void dispose() {
    _alive = false;
    _reconnectTimer?.cancel();
    _socket?.close();
    _terminal.onOutput = null;
    _terminal.onResize = null;
    super.dispose();
  }

  /// Pauses live output like web's live-toggle: the socket closes but
  /// the terminal buffer stays, so resuming repaints into the frozen
  /// screen instead of blanking it.
  void _toggleLive() {
    if (_paused) {
      _paused = false;
      _attempt = 0;
      _connect();
    } else {
      _paused = true;
      _reconnectTimer?.cancel();
      _socket?.close();
      _socket = null;
      setState(() => _status = '已暂停实时刷新');
    }
  }

  void _sendOutput(String data) {
    final socket = _socket;
    if (socket == null || !socket.isOpen) return;
    socket.add(jsonEncode({'type': 'input', 'data': data}));
  }

  void _sendResize(int cols, int rows, int pixelWidth, int pixelHeight) {
    final socket = _socket;
    if (socket == null || !socket.isOpen) return;
    socket.add(jsonEncode({'type': 'resize', 'cols': cols, 'rows': rows}));
  }

  Future<void> _connect() async {
    final cols = _terminal.viewWidth.clamp(20, 400);
    final rows = _terminal.viewHeight.clamp(5, 200);
    final uri = widget.api.wsUri(
      '/ws/terminal/${encodePaneRef(widget.pane.ref)}',
      {'cols': '$cols', 'rows': '$rows'},
    );
    setState(() => _status = '连接中…');
    try {
      final socket = await AppSocket.connect(uri, headers: widget.api.wsHeaders);
      if (!mounted || !_alive) {
        await socket.close();
        return;
      }
      _socket = socket;
      setState(() => _status = '已连接 · ${widget.pane.label}');
      socket.stream.listen(
        (data) {
          if (!_alive) return;
          if (data is String) {
            _terminal.write(data);
          } else if (data is List<int>) {
            _terminal.write(utf8.decode(data, allowMalformed: true));
          }
        },
        onDone: _reconnect,
        onError: (_) => _reconnect(),
        cancelOnError: true,
      );
    } catch (error) {
      if (!mounted || !_alive) return;
      setState(() => _status = '连接失败：$error');
      _reconnect();
    }
  }

  void _reconnect() {
    if (!_alive || _paused) return;
    _socket = null;
    setState(() => _status = '连接断开，正在重连…');
    _attempt += 1;
    final delay = Duration(milliseconds: 600 * (_attempt.clamp(1, 5)));
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(delay, () {
      if (_alive && mounted && !_paused) _connect();
    });
  }

  Widget _terminalView() => TerminalView(
        _terminal,
        autofocus: !widget.embedded,
        padding: const EdgeInsets.all(6),
        theme: const TerminalTheme(
          cursor: Color(0xFFFAF9F5),
          selection: Color(0x665A574F),
          foreground: Color(0xFFE7E5DF),
          background: Color(0xFF111111),
          black: Color(0xFF2B2B2B),
          red: Color(0xFFE06C60),
          green: Color(0xFF8FBF6F),
          yellow: Color(0xFFE2C46B),
          blue: Color(0xFF6FA8DC),
          magenta: Color(0xFFC88BD1),
          cyan: Color(0xFF6FC5C0),
          white: Color(0xFFD8D6CF),
          brightBlack: Color(0xFF6B6B6B),
          brightRed: Color(0xFFF08C80),
          brightGreen: Color(0xFFA8D98A),
          brightYellow: Color(0xFFF2D98A),
          brightBlue: Color(0xFF8FC1F0),
          brightMagenta: Color(0xFFDDA8E6),
          brightCyan: Color(0xFF8FDCD7),
          brightWhite: Color(0xFFFFFFFF),
          searchHitBackground: Color(0x66C96442),
          searchHitBackgroundCurrent: Color(0x99C96442),
          searchHitForeground: Color(0xFF1A1A1A),
        ),
      );

  @override
  Widget build(BuildContext context) {
    if (widget.embedded) {
      return Container(color: const Color(0xFF111111), child: _terminalView());
    }
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(widget.pane.label, maxLines: 1, overflow: TextOverflow.ellipsis),
            Text(_status, style: TextStyle(fontSize: 11, fontWeight: FontWeight.w400, color: AppTheme.faint)),
          ],
        ),
        actions: [
          IconButton(
            key: const Key('terminal-live-toggle'),
            tooltip: _paused ? '继续实时刷新' : '暂停实时刷新',
            onPressed: _toggleLive,
            icon: Icon(_paused ? Icons.play_arrow : Icons.pause, size: 20),
          ),
          IconButton(
            tooltip: '重新连接',
            onPressed: () {
              _socket?.close();
              _socket = null;
              _attempt = 0;
              _connect();
            },
            icon: const Icon(Icons.refresh, size: 20),
          ),
        ],
      ),
      backgroundColor: const Color(0xFF111111),
      body: SafeArea(child: _terminalView()),
    );
  }
}
