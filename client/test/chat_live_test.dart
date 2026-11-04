import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/api/api_client.dart';
import 'package:tmuxweb_client/models/chat_source.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/models/transcript_cache.dart';
import 'package:tmuxweb_client/screens/chat_screen.dart';
import 'package:tmuxweb_client/services/screen_stream.dart';
import 'package:tmuxweb_client/theme.dart';

class LiveFakeSource implements ChatSource {
  LiveFakeSource({required this.transcript, this.sendError, Stream<String>? frames})
      : _frames = frames;

  Transcript transcript;
  final Object? sendError;
  final Stream<String>? _frames;
  int fetches = 0;

  @override
  String get cacheKey => 'pane:live';

  @override
  String get title => '实时会话';

  @override
  String get subtitle => 'live';

  @override
  bool get readOnly => false;

  @override
  Future<Transcript> fetch({bool refresh = false}) async {
    fetches += 1;
    return transcript;
  }

  @override
  Stream<String> screenStream() => _frames ?? const Stream.empty();

  // Same ChatSource-default overrides as chat_screen_test's FakeSource.
  @override
  Stream<bool> screenReconnecting() => const Stream.empty();

  @override
  bool get isScreenReconnecting => false;

  @override
  Api? get chatApi => null;

  @override
  String? get paneRef => null;

  @override
  String get panePath => '';

  @override
  String? get numberLabel => null;

  @override
  Future<void> send(String text, {bool submit = true}) async {
    if (sendError != null) throw sendError!;
  }

  @override
  Future<String> stop() async => 'Escape';
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  group('capture helpers', () {
    test('stripAnsi removes color and OSC sequences', () {
      expect(stripAnsi('\x1b[32mgreen\x1b[0m and \x1b]8;;http://x\x07link\x1b]8;;\x07'), 'green and link');
    });

    test('trimCapture keeps the tail under the line cap', () {
      final text = List.generate(700, (i) => 'line $i').join('\n');
      final trimmed = trimCapture(text);
      expect(trimmed.startsWith('… 只显示最近 600 行 …'), isTrue);
      expect(trimmed.split('\n').length, 601);
      expect(trimmed, contains('line 699'));
    });

    test('trimCapture keeps the tail under the byte cap', () {
      final text = 'x' * 200000;
      expect(trimCapture(text).length, lessThanOrEqualTo(160000));
    });
  });

  group('ScreenStream over a real local socket', () {
    test('emits captures, skips malformed frames, reconnects after a drop', () async {
      var connections = 0;
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      server.listen((request) async {
        if (!WebSocketTransformer.isUpgradeRequest(request)) return;
        final socket = await WebSocketTransformer.upgrade(request);
        connections += 1;
        if (connections == 1) {
          socket.add('not json');
          socket.add(jsonEncode({'type': 'other', 'content': 'ignored'}));
          socket.add(jsonEncode({'type': 'capture', 'content': 'first frame'}));
          await socket.close();
        } else {
          socket.add(jsonEncode({'type': 'capture', 'content': 'second frame'}));
        }
      });
      try {
        final stream = ScreenStream(uri: Uri.parse('ws://127.0.0.1:${server.port}/ws/panes/%7'), headers: {});
        final received = <String>[];
        final sub = stream.stream.listen(received.add);
        stream.start();
        final deadline = DateTime.now().add(const Duration(seconds: 8));
        while (received.length < 2 && DateTime.now().isBefore(deadline)) {
          await Future.delayed(const Duration(milliseconds: 100));
        }
        expect(received, ['first frame', 'second frame']);
        expect(connections, 2);
        await sub.cancel();
        await stream.close();
      } finally {
        await server.close();
      }
    });
  });

  group('ChatScreen live controls', () {
    testWidgets('screen frames replace content, pause stops the stream, resume restarts it', (tester) async {
      final frames = StreamController<String>.broadcast();
      addTearDown(frames.close);
      final source = LiveFakeSource(
        transcript: Transcript(mode: 'screen', content: 'old output'),
        frames: frames.stream,
      );
      await tester.pumpWidget(MaterialApp(
        theme: AppTheme.light(),
        home: ChatScreen(source: source, poll: Duration.zero, cache: TranscriptCache()),
      ));
      await tester.pumpAndSettle();
      expect(find.text('old output'), findsOneWidget);

      frames.add('\x1b[32mgreen\x1b[0m new output');
      await tester.pumpAndSettle();
      expect(find.text('green new output'), findsOneWidget);
      expect(find.text('old output'), findsNothing);

      frames.add('second frame');
      await tester.pumpAndSettle();
      expect(find.text('second frame'), findsOneWidget);
      expect(find.text('green new output'), findsNothing);

      await tester.tap(find.byKey(const Key('chat-live-toggle')));
      await tester.pumpAndSettle();
      expect(find.text('已暂停'), findsOneWidget);
      frames.add('while paused');
      await tester.pumpAndSettle();
      expect(find.text('while paused'), findsNothing);

      await tester.tap(find.byKey(const Key('chat-live-toggle')));
      await tester.pumpAndSettle();
      expect(find.text('已暂停'), findsNothing);
      frames.add('after resume');
      await tester.pumpAndSettle();
      expect(find.text('after resume'), findsOneWidget);
    });

    testWidgets('pause stops transcript polling and resume restarts it', (tester) async {
      final source = LiveFakeSource(
        transcript: Transcript(mode: 'transcript', messages: [
          ChatMessage(id: 'u1', role: 'user', text: 'hi'),
        ]),
      );
      await tester.pumpWidget(MaterialApp(
        theme: AppTheme.light(),
        home: ChatScreen(source: source, cache: TranscriptCache()),
      ));
      await tester.pumpAndSettle();
      final countAfterSettle = source.fetches;

      await tester.tap(find.byKey(const Key('chat-live-toggle')));
      await tester.pumpAndSettle();
      expect(find.text('已暂停'), findsOneWidget);
      await tester.pump(const Duration(seconds: 5));
      await tester.pump();
      expect(source.fetches, countAfterSettle);

      await tester.tap(find.byKey(const Key('chat-live-toggle')));
      await tester.pumpAndSettle();
      expect(find.text('已暂停'), findsNothing);
      await tester.pump(const Duration(seconds: 2));
      await tester.pump();
      expect(source.fetches, greaterThan(countAfterSettle));
    });

    testWidgets('failed send puts the text back in the composer', (tester) async {
      final source = LiveFakeSource(
        transcript: Transcript(mode: 'transcript', messages: []),
        sendError: ApiException(500, '发送失败测试'),
      );
      await tester.pumpWidget(MaterialApp(
        theme: AppTheme.light(),
        home: ChatScreen(source: source, poll: Duration.zero, cache: TranscriptCache()),
      ));
      await tester.pumpAndSettle();

      final composer = find.byType(TextField).first;
      await tester.enterText(composer, 'hello fail');
      await tester.tap(find.byKey(const Key('chat-send')));
      await tester.pumpAndSettle();

      expect(find.textContaining('发送失败测试'), findsOneWidget);
      // 失败的消息留在记录里可重发，输入框清空（避免重发成两条）。
      expect(find.byKey(const Key('chat-resend')), findsOneWidget);
      expect(tester.widget<TextField>(composer).controller?.text, '');
    });
  });

  group('ScreenStream reconnect state', () {
    test('reconnecting is true after a drop and false once reconnected', () async {
      var connections = 0;
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      server.listen((request) async {
        if (!WebSocketTransformer.isUpgradeRequest(request)) return;
        final socket = await WebSocketTransformer.upgrade(request);
        connections += 1;
        if (connections == 1) {
          socket.add(jsonEncode({'type': 'capture', 'content': 'first frame'}));
          await socket.close();
        } else {
          socket.add(jsonEncode({'type': 'capture', 'content': 'second frame'}));
        }
      });
      try {
        final stream = ScreenStream(uri: Uri.parse('ws://127.0.0.1:${server.port}/ws/panes/%7'), headers: {});
        final received = <String>[];
        final states = <bool>[];
        final frameSub = stream.stream.listen(received.add);
        final stateSub = stream.reconnecting.listen(states.add);
        stream.start();
        final deadline = DateTime.now().add(const Duration(seconds: 8));
        while (received.length < 2 && DateTime.now().isBefore(deadline)) {
          await Future.delayed(const Duration(milliseconds: 100));
        }
        expect(received, ['first frame', 'second frame']);
        expect(states, [true, false]);
        await frameSub.cancel();
        await stateSub.cancel();
        await stream.close();
      } finally {
        await server.close();
      }
    });

    testWidgets('ChatScreen shows a 重连中… badge while reconnecting', (tester) async {
      final frames = StreamController<String>.broadcast();
      final reconnecting = StreamController<bool>.broadcast();
      addTearDown(frames.close);
      addTearDown(reconnecting.close);
      final source = _ReconnectSource(frames: frames.stream, reconnecting: reconnecting.stream);
      await tester.pumpWidget(MaterialApp(
        theme: AppTheme.light(),
        home: ChatScreen(source: source, poll: Duration.zero, cache: TranscriptCache()),
      ));
      await tester.pumpAndSettle();
      expect(find.text('重连中…'), findsNothing);

      reconnecting.add(true);
      await tester.pumpAndSettle();
      expect(find.text('重连中…'), findsOneWidget);

      reconnecting.add(false);
      await tester.pumpAndSettle();
      expect(find.text('重连中…'), findsNothing);
    });
  });
}

class _ReconnectSource implements ChatSource {
  _ReconnectSource({required this.frames, required this.reconnecting});

  final Stream<String> frames;
  final Stream<bool> reconnecting;

  @override
  String get cacheKey => 'pane:reconnect';

  @override
  String get title => '重连会话';

  @override
  String get subtitle => '';

  @override
  bool get readOnly => false;

  @override
  Future<Transcript> fetch({bool refresh = false}) async => Transcript(mode: 'screen', content: 'screen output');

  @override
  Stream<String> screenStream() => frames;

  @override
  Stream<bool> screenReconnecting() => reconnecting;

  @override
  bool get isScreenReconnecting => false;

  @override
  Api? get chatApi => null;

  @override
  String? get paneRef => null;

  @override
  String get panePath => '';

  @override
  String? get numberLabel => null;

  @override
  Future<void> send(String text, {bool submit = true}) async {}

  @override
  Future<String> stop() async => 'Escape';
}
