import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:xterm/xterm.dart';

import '../api/api_client.dart';
import '../models/models.dart';
import '../services/app_socket.dart';
import '../theme.dart';

/// Real PTY terminal for one pane (local or SSH), driven by the server's
/// `/ws/terminal/{pane}` socket.
///
/// Status texts, the 5-attempt reconnect policy, the pre-connect input
/// buffer, the touch key row and the 9–22px persisted font size all
/// mirror web/term.html. The socket itself still goes through
/// [TerminalApi.wsUri]/[TerminalApi.wsHeaders] (cookie + bearer <redacted> —
/// the POST /api/session exchange that makes native sockets work), which
/// this screen must not regress.
class TerminalScreen extends StatefulWidget {
  const TerminalScreen(
      {super.key, required this.api, required this.pane, this.embedded = false, this.onOpenInConsole});

  final TerminalApi api;
  final Pane pane;

  /// Web's term page links back to the console. Supplied by full-page
  /// terminal pushes that can build a console route; null (and hence
  /// no button) for embedded terminals and for pushes without one.
  final VoidCallback? onOpenInConsole;

  /// Inside a grid cell there is no room for the page chrome: only
  /// the terminal view, like web's /term?embed=1 iframe.
  final bool embedded;

  @override
  State<TerminalScreen> createState() => _TerminalScreenState();
}

/// SharedPreferences key for the font size; web uses the same string
/// in localStorage ('tmux-term-font'), so the value moves with
/// settings export/import verbatim.
const String terminalFontPrefsKey = 'tmux-term-font';

class _TerminalScreenState extends State<TerminalScreen> with WidgetsBindingObserver {
  late final Terminal _terminal = Terminal(maxLines: 10000)
    ..onOutput = _sendOutput
    ..onResize = _sendResize;

  AppSocket? _socket;
  String _status = '连接中…';
  bool _alive = true;
  bool _paused = false;

  /// Web's retry counter: capped at 5, 1200ms × attempt. Reset on a
  /// successful connect and on a manual retry, like web's retry = 0
  /// in onopen.
  int _attempt = 0;
  Timer? _reconnectTimer;

  /// Typed before the socket is up: web's `pending`, flushed on open
  /// instead of being dropped.
  String _pending = '';

  /// Touch key row: web shows .keys only on touch devices
  /// (hover:none + pointer:coarse); Flutter mirrors that with the
  /// mobile target platforms, plus a toggle for every platform.
  late bool _showKeys = defaultTargetPlatform == TargetPlatform.iOS || defaultTargetPlatform == TargetPlatform.android;

  double _fontSize = 13;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _loadFont();
    _connect();
  }

  @override
  void dispose() {
    _alive = false;
    WidgetsBinding.instance.removeObserver(this);
    _reconnectTimer?.cancel();
    _socket?.close();
    _terminal.onOutput = null;
    _terminal.onResize = null;
    super.dispose();
  }

  /// Web's visibilitychange: when the app comes back, reconnect if
  /// the socket is gone (even after the 5 attempts were exhausted —
  /// web reconnects on visibility regardless of the retry counter,
  /// and the counter resets on the next successful open).
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && _alive && !_paused && _socket == null) {
      _reconnectTimer?.cancel();
      _connect();
    }
  }

  Future<void> _loadFont() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final saved = prefs.get(terminalFontPrefsKey);
      final size = saved is num ? saved.toDouble() : null;
      if (!mounted) return;
      setState(() {
        _fontSize = size != null && size >= 9 && size <= 22
            ? size
            // Web's default: 11.5 on coarse pointers, 13 otherwise.
            : (defaultTargetPlatform == TargetPlatform.iOS || defaultTargetPlatform == TargetPlatform.android ? 11.5 : 13);
      });
    } catch (_) {
      // Keep the default size; a failed preference read must not
      // break the terminal.
    }
  }

  Future<void> _setFontSize(double size) async {
    final clamped = size.clamp(9.0, 22.0);
    setState(() => _fontSize = clamped);
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setDouble(terminalFontPrefsKey, clamped);
    } catch (_) {
      // Font size is a preference, not data: a failed persist must
      // not break the terminal.
    }
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

  void _manualRetry() {
    _reconnectTimer?.cancel();
    _socket?.close();
    _socket = null;
    _attempt = 0;
    _connect();
  }

  void _sendOutput(String data) {
    final socket = _socket;
    if (socket == null || !socket.isOpen) {
      // Web buffers instead of dropping: `pending += data`.
      _pending += data;
      return;
    }
    socket.add(jsonEncode({'type': 'input', 'data': data}));
  }

  /// Touch key row and paste go through the same buffered send as
  /// typed input, like web's data-key buttons (else pending += data).
  void _sendKey(String data) => _sendOutput(data);

  Future<void> _paste() async {
    try {
      final data = await Clipboard.getData(Clipboard.kTextPlain);
      final text = data?.text;
      if (text == null || text.isEmpty) return;
      _sendOutput(text.replaceAll(RegExp(r'\r?\n'), '\r'));
    } catch (_) {
      if (mounted) setState(() => _status = '粘贴被系统拦截');
    }
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
      _attempt = 0;
      if (_pending.isNotEmpty) {
        socket.add(jsonEncode({'type': 'input', 'data': _pending}));
        _pending = '';
      }
      setState(() => _status = '已连接');
      socket.stream.listen(
        (data) {
          if (!_alive) return;
          if (data is String) {
            _terminal.write(data);
          } else if (data is List<int>) {
            _terminal.write(utf8.decode(data, allowMalformed: true));
          }
        },
        onDone: () => _onSocketClosed(socket),
        onError: (_) => _onSocketClosed(socket),
        cancelOnError: true,
      );
    } catch (error) {
      if (!mounted || !_alive) return;
      _scheduleReconnect();
    }
  }

  void _onSocketClosed(AppSocket socket) {
    // onDone and onError can both fire for one close; only the
    // current socket schedules a reconnect, once.
    if (_socket != socket) return;
    _socket = null;
    _scheduleReconnect();
  }

  /// Web's onclose policy: '已断开', then while retry < 5 schedule
  /// 1200ms × retry with '重连中 N/5'; after 5 attempts, '连接失败'.
  /// A manual retry (the AppBar refresh button) is still possible
  /// after failure — web has no such button (its only post-failure
  /// path is visibilitychange), so the button is a deliberate
  /// Flutter addition, kept from the previous screen.
  void _scheduleReconnect() {
    if (!_alive || _paused) return;
    if (_attempt >= 5) {
      setState(() => _status = '连接失败');
      return;
    }
    _attempt += 1;
    setState(() => _status = '重连中 $_attempt/5');
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(Duration(milliseconds: 1200 * _attempt), () {
      if (_alive && mounted && !_paused) _connect();
    });
  }

  bool get _connected => _status == '已连接';

  /// Status text + live dot, shared by the full page and the embedded
  /// view (web's #state + .dot, compact in embedded mode).
  Widget _statusRow({bool compact = false}) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          key: const Key('terminal-dot'),
          width: 8,
          height: 8,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: _connected ? AppTheme.green : AppTheme.faint,
          ),
        ),
        const SizedBox(width: 6),
        Flexible(
          child: Text(
            _status,
            key: const Key('terminal-status'),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              fontSize: 11,
              fontWeight: FontWeight.w400,
              color: AppTheme.faint,
            ),
          ),
        ),
      ],
    );
  }

  Widget _terminalView() => TerminalView(
        _terminal,
        autofocus: !widget.embedded,
        padding: const EdgeInsets.all(6),
        textStyle: TerminalStyle(fontSize: _fontSize),
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

  /// Web term.html's .keys bar, in the same order and with the same
  /// byte sequences (esc/tab/^C/^D/enter/arrows + paste).
  Widget _keyRow() {
    Widget keyButton(String label, String data, {Key? key}) => Padding(
          padding: const EdgeInsets.only(right: 6),
          child: OutlinedButton(
            key: key,
            onPressed: () => _sendKey(data),
            style: OutlinedButton.styleFrom(
              minimumSize: const Size(42, 36),
              padding: const EdgeInsets.symmetric(horizontal: 9),
              side: const BorderSide(color: Color(0xFF454545)),
              foregroundColor: const Color(0xFFEEEEEE),
              backgroundColor: const Color(0xFF292929),
              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
            ),
            child: Text(label, style: const TextStyle(fontSize: 12, fontFamily: 'monospace')),
          ),
        );
    return Container(
      color: const Color(0xFF181818),
      padding: const EdgeInsets.all(6),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Row(children: [
          keyButton('Esc', '\u001b', key: const Key('terminal-key-esc')),
          keyButton('Tab', '\t', key: const Key('terminal-key-tab')),
          keyButton('^C', '\u0003', key: const Key('terminal-key-ctrlc')),
          keyButton('^D', '\u0004', key: const Key('terminal-key-ctrld')),
          keyButton('Enter', '\r', key: const Key('terminal-key-enter')),
          keyButton('←', '\u001b[D', key: const Key('terminal-key-left')),
          keyButton('↓', '\u001b[B', key: const Key('terminal-key-down')),
          keyButton('↑', '\u001b[A', key: const Key('terminal-key-up')),
          keyButton('→', '\u001b[C', key: const Key('terminal-key-right')),
          Padding(
            padding: const EdgeInsets.only(right: 6),
            child: OutlinedButton(
              key: const Key('terminal-key-paste'),
              onPressed: _paste,
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(64, 36),
                padding: const EdgeInsets.symmetric(horizontal: 9),
                side: const BorderSide(color: Color(0xFF454545)),
                foregroundColor: const Color(0xFFEEEEEE),
                backgroundColor: const Color(0xFF292929),
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
              ),
              child: const Text('粘贴', style: TextStyle(fontSize: 12)),
            ),
          ),
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (widget.embedded) {
      return Container(
        color: const Color(0xFF111111),
        child: SafeArea(
          top: false,
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(8, 4, 8, 2),
                child: Align(alignment: Alignment.centerLeft, child: _statusRow(compact: true)),
              ),
              Expanded(child: _terminalView()),
              if (_showKeys) _keyRow(),
            ],
          ),
        ),
      );
    }
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(widget.pane.label, maxLines: 1, overflow: TextOverflow.ellipsis),
            _statusRow(),
          ],
        ),
        actions: [
          IconButton(
            key: const Key('terminal-font-down'),
            tooltip: '减小字体',
            onPressed: () => _setFontSize(_fontSize - 1),
            icon: const Icon(Icons.text_decrease, size: 20),
          ),
          IconButton(
            key: const Key('terminal-font-up'),
            tooltip: '增大字体',
            onPressed: () => _setFontSize(_fontSize + 1),
            icon: const Icon(Icons.text_increase, size: 20),
          ),
          IconButton(
            key: const Key('terminal-keys-toggle'),
            tooltip: _showKeys ? '隐藏按键行' : '显示按键行',
            onPressed: () => setState(() => _showKeys = !_showKeys),
            icon: const Icon(Icons.keyboard, size: 20),
          ),
          IconButton(
            key: const Key('terminal-live-toggle'),
            tooltip: _paused ? '继续实时刷新' : '暂停实时刷新',
            onPressed: _toggleLive,
            icon: Icon(_paused ? Icons.play_arrow : Icons.pause, size: 20),
          ),
          IconButton(
            tooltip: '重新连接',
            onPressed: _manualRetry,
            icon: const Icon(Icons.refresh, size: 20),
          ),
          if (widget.onOpenInConsole != null)
            IconButton(
              key: const Key('terminal-open-console'),
              tooltip: '在控制台打开',
              onPressed: widget.onOpenInConsole,
              icon: const Icon(Icons.chat_bubble_outline, size: 20),
            ),
        ],
      ),
      backgroundColor: const Color(0xFF111111),
      body: SafeArea(
        child: Column(children: [
          Expanded(child: _terminalView()),
          if (_showKeys) _keyRow(),
        ]),
      ),
    );
  }
}
