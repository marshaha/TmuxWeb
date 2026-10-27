import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/models/chat_source.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/models/transcript_cache.dart';
import 'package:tmuxweb_client/screens/chat_screen.dart';
import 'package:tmuxweb_client/theme.dart';

class FakeSource implements ChatSource {
  FakeSource({this.readOnly = false, Transcript? transcript}) : _transcript = transcript ?? _default;

  static final _default = Transcript(mode: 'transcript', provider: 'codex', mtime: 1, messages: [
    ChatMessage(id: 'u1', role: 'user', text: '给我一个表格'),
    ChatMessage(
      id: 'a1',
      role: 'assistant',
      text: '说明\n\n```python\nprint("hi")\n```\n\n| 主机 | pane |\n| --- | --- |\n| ctyun | %0 |',
    ),
  ]);

  Transcript _transcript;
  @override
  final bool readOnly;
  final List<String> sent = [];

  @override
  String get cacheKey => 'pane:test';

  @override
  String get title => '测试会话';

  @override
  String get subtitle => 'codex · 本地';

  @override
  Future<Transcript> fetch({bool refresh = false}) async => _transcript;

  @override
  Future<void> send(String text, {bool submit = true}) async {
    sent.add(text);
    _transcript = Transcript(
      mode: 'transcript',
      provider: 'codex',
      mtime: 'v${sent.length}',
      messages: [..._transcript.messages, ChatMessage(id: 'n${sent.length}', role: 'user', text: text)],
    );
  }
}

void main() {
  late List<String> clipboard;

  setUp(() {
    clipboard = [];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      SystemChannels.platform,
      (call) async {
        if (call.method == 'Clipboard.setData') clipboard.add((call.arguments as Map)['text'] as String);
        return null;
      },
    );
  });

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, null);
  });

  Widget wrap(Widget child) => MaterialApp(theme: AppTheme.light(), home: child);

  testWidgets('renders messages, code and table blocks', (tester) async {
    await tester.pumpWidget(wrap(ChatScreen(source: FakeSource(), poll: Duration.zero)));
    await tester.pumpAndSettle();

    expect(find.text('给我一个表格'), findsOneWidget);
    expect(find.text('说明'), findsOneWidget);
    expect(find.text('PYTHON'), findsOneWidget);
    expect(find.text('TABLE'), findsOneWidget);
    expect(find.text('ctyun'), findsOneWidget); // table rendered, not source
    expect(find.text('复制全部'), findsNWidgets(2));
    expect(find.text('复制'), findsNWidgets(2));
  });

  testWidgets('copy-all puts the whole message on the clipboard', (tester) async {
    await tester.pumpWidget(wrap(ChatScreen(source: FakeSource(), poll: Duration.zero)));
    await tester.pumpAndSettle();

    await tester.tap(find.text('复制全部').last);
    await tester.pump();
    expect(clipboard.single, contains('| 主机 | pane |'));
    expect(find.text('已复制'), findsOneWidget);
    await tester.pump(const Duration(seconds: 2)); // let the "copied" reset timer finish
  });

  testWidgets('block copy uses the code source and TSV for tables', (tester) async {
    await tester.pumpWidget(wrap(ChatScreen(source: FakeSource(), poll: Duration.zero)));
    await tester.pumpAndSettle();

    final copies = find.text('复制');
    expect(copies, findsNWidgets(2));
    await tester.tap(copies.first);
    await tester.pump();
    expect(clipboard.last, 'print("hi")');

    await tester.tap(find.text('复制').last);
    await tester.pump();
    expect(clipboard.last, '主机\tpane\nctyun\t%0');
    await tester.pump(const Duration(seconds: 2));
  });

  testWidgets('table toggles between rendered and source', (tester) async {
    await tester.pumpWidget(wrap(ChatScreen(source: FakeSource(), poll: Duration.zero)));
    await tester.pumpAndSettle();

    expect(find.text('ctyun'), findsOneWidget);
    await tester.tap(find.text('源码'));
    await tester.pumpAndSettle();
    expect(find.text('| 主机 | pane |\n| --- | --- |\n| ctyun | %0 |'), findsOneWidget);
    expect(find.text('渲染'), findsOneWidget);
    await tester.tap(find.text('渲染'));
    await tester.pumpAndSettle();
    expect(find.text('ctyun'), findsOneWidget);
  });

  testWidgets('read-only source hides the composer and shows the banner', (tester) async {
    await tester.pumpWidget(wrap(ChatScreen(source: FakeSource(readOnly: true), poll: Duration.zero)));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('read-only-banner')), findsOneWidget);
    expect(find.text('只读分享：可以查看内容，但不能发送消息。'), findsOneWidget);
    expect(find.byKey(const Key('chat-composer')), findsNothing);
    expect(find.text('只读'), findsOneWidget);
  });

  testWidgets('sending shows the message and clears the field', (tester) async {
    final source = FakeSource();
    await tester.pumpWidget(wrap(ChatScreen(source: source, poll: Duration.zero)));
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('chat-composer')), '继续处理');
    await tester.tap(find.byKey(const Key('chat-send')));
    await tester.pumpAndSettle();

    expect(source.sent, ['继续处理']);
    expect(find.text('继续处理'), findsOneWidget);
    expect(tester.widget<TextField>(find.byKey(const Key('chat-composer'))).controller!.text, isEmpty);
  });

  testWidgets('a cached conversation paints before the network answers', (tester) async {
    final cache = TranscriptCache();
    cache.write('pane:test', Transcript(mode: 'transcript', provider: 'codex', mtime: 9, messages: [
      ChatMessage(id: 'c1', role: 'assistant', text: '缓存里的旧回答'),
    ]));
    final source = _SlowSource();

    await tester.pumpWidget(wrap(ChatScreen(source: source, poll: Duration.zero, cache: cache)));
    await tester.pump(); // one frame: the fetch is still pending

    expect(find.text('缓存里的旧回答'), findsOneWidget);
    expect(find.text('更新中…'), findsOneWidget);
    expect(source.calls, 1);

    source.complete(Transcript(mode: 'transcript', provider: 'codex', mtime: 10, messages: [
      ChatMessage(id: 'c1', role: 'assistant', text: '缓存里的旧回答'),
      ChatMessage(id: 'c2', role: 'assistant', text: '新到的回答'),
    ]));
    await tester.pumpAndSettle();

    expect(find.text('新到的回答'), findsOneWidget);
    expect(find.text('更新中…'), findsNothing);
    expect(cache.read('pane:test')!.transcript.messages, hasLength(2));
  });

  testWidgets('the draft is kept per conversation', (tester) async {
    final cache = TranscriptCache();
    await tester.pumpWidget(wrap(ChatScreen(source: FakeSource(), poll: Duration.zero, cache: cache)));
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('chat-composer')), '还没发出去的草稿');
    await tester.pump();
    expect(cache.draft('pane:test'), '还没发出去的草稿');

    await tester.pumpWidget(wrap(const SizedBox()));
    await tester.pumpAndSettle();

    await tester.pumpWidget(wrap(ChatScreen(source: FakeSource(), poll: Duration.zero, cache: cache)));
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(find.byKey(const Key('chat-composer'))).controller!.text, '还没发出去的草稿');
  });

  testWidgets('the cache keeps a bounded working set', (tester) async {
    final cache = TranscriptCache(capacity: 2);
    for (final key in ['a', 'b', 'c']) {
      cache.write(key, Transcript(mode: 'transcript', mtime: key, messages: const []));
    }
    expect(cache.length, 2);
    expect(cache.has('a'), isFalse);
    expect(cache.has('c'), isTrue);
  });

  testWidgets('screen-mode panes render the raw terminal text', (tester) async {
    final source = FakeSource(
      transcript: Transcript(mode: 'screen', mtime: 'x', content: 'zsh\$ ls -la\nresults/', messages: const []),
    );
    await tester.pumpWidget(wrap(ChatScreen(source: source, poll: Duration.zero)));
    await tester.pumpAndSettle();

    expect(find.textContaining('zsh'), findsOneWidget);
    expect(find.text('复制全部'), findsNothing);
  });

  testWidgets('a failed send surfaces the error and drops the pending bubble', (tester) async {
    final source = _FailingSource();
    await tester.pumpWidget(wrap(ChatScreen(source: source, poll: Duration.zero)));
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('chat-composer')), 'hi');
    await tester.tap(find.byKey(const Key('chat-send')));
    await tester.pumpAndSettle();

    expect(find.textContaining('该会话只读，不能发送消息'), findsOneWidget);
    expect(find.text('hi'), findsNothing);
  });
}

class _SlowSource extends FakeSource {
  int calls = 0;
  final _completer = Completer<Transcript>();

  void complete(Transcript transcript) => _completer.complete(transcript);

  @override
  Future<Transcript> fetch({bool refresh = false}) {
    calls += 1;
    return _completer.future;
  }
}

class _FailingSource extends FakeSource {
  @override
  Future<void> send(String text, {bool submit = true}) async => throw Exception('该会话只读，不能发送消息');
}
