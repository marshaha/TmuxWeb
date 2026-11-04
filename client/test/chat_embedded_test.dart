import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/api/api_client.dart';
import 'package:tmuxweb_client/models/chat_source.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/models/transcript_cache.dart';
import 'package:tmuxweb_client/screens/chat_screen.dart';
import 'package:tmuxweb_client/services/pointer_kind.dart';
import 'package:tmuxweb_client/theme.dart';

/// Counting transcript source: every poll tick is one fetch, and the
/// transcript never changes, so refreshes after the first one do not
/// repaint — fetch counts are the poll cadence, nothing else.
class CountingSource implements ChatSource {
  CountingSource({this.key = 'pane:embedded'});

  final String key;
  int fetches = 0;

  Transcript get transcript => Transcript(mode: 'transcript', messages: [
        ChatMessage(id: 'u1', role: 'user', text: 'hi'),
        ChatMessage(id: 'a1', role: 'assistant', text: 'hello back'),
      ]);

  @override
  String get cacheKey => key;

  @override
  String get title => '嵌入会话';

  @override
  String get subtitle => '';

  @override
  bool get readOnly => false;

  @override
  Future<Transcript> fetch({bool refresh = false}) async {
    fetches += 1;
    return transcript;
  }

  @override
  Stream<String> screenStream() => const Stream.empty();

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
  Future<void> send(String text, {bool submit = true}) async {}

  @override
  Future<String> stop() async => 'Escape';
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  tearDown(() => debugDefaultTargetPlatformOverride = null);

  /// Walks the app lifecycle to [target] using only transitions
  /// Flutter 3.47 allows (AppLifecycleListener asserts on direct
  /// paused <-> resumed jumps): visible -> hidden is
  /// resumed -> inactive -> hidden -> paused, and back is
  /// paused -> hidden -> inactive -> resumed. Zero-duration pumps
  /// between steps pass no fake time, so a restarted timer's anchor
  /// is effectively the moment the walk completes.
  Future<void> goLifecycle(WidgetTester tester, AppLifecycleState target) async {
    AppLifecycleState? current = tester.binding.lifecycleState;
    if (current == target) return;
    List<AppLifecycleState> path;
    switch (target) {
      case AppLifecycleState.paused:
        path = switch (current) {
          AppLifecycleState.resumed => [AppLifecycleState.inactive, AppLifecycleState.hidden, AppLifecycleState.paused],
          AppLifecycleState.inactive => [AppLifecycleState.hidden, AppLifecycleState.paused],
          AppLifecycleState.hidden => [AppLifecycleState.paused],
          AppLifecycleState.detached => [AppLifecycleState.resumed, AppLifecycleState.inactive, AppLifecycleState.hidden, AppLifecycleState.paused],
          _ => [AppLifecycleState.paused],
        };
      case AppLifecycleState.resumed:
        path = switch (current) {
          AppLifecycleState.paused => [AppLifecycleState.hidden, AppLifecycleState.inactive, AppLifecycleState.resumed],
          AppLifecycleState.hidden => [AppLifecycleState.inactive, AppLifecycleState.resumed],
          AppLifecycleState.inactive => [AppLifecycleState.resumed],
          _ => [AppLifecycleState.resumed],
        };
      case AppLifecycleState.inactive:
        path = switch (current) {
          AppLifecycleState.resumed => [AppLifecycleState.inactive],
          AppLifecycleState.hidden => [AppLifecycleState.inactive],
          AppLifecycleState.paused => [AppLifecycleState.hidden, AppLifecycleState.inactive],
          AppLifecycleState.detached => [AppLifecycleState.resumed, AppLifecycleState.inactive],
          _ => [AppLifecycleState.inactive],
        };
      case AppLifecycleState.hidden:
        path = switch (current) {
          AppLifecycleState.resumed => [AppLifecycleState.inactive, AppLifecycleState.hidden],
          AppLifecycleState.paused => [AppLifecycleState.hidden],
          AppLifecycleState.inactive => [AppLifecycleState.hidden],
          _ => [AppLifecycleState.hidden],
        };
      case AppLifecycleState.detached:
        path = switch (current) {
          AppLifecycleState.paused => [AppLifecycleState.detached],
          AppLifecycleState.hidden => [AppLifecycleState.paused, AppLifecycleState.detached],
          AppLifecycleState.inactive => [AppLifecycleState.hidden, AppLifecycleState.paused, AppLifecycleState.detached],
          AppLifecycleState.resumed => [AppLifecycleState.inactive, AppLifecycleState.hidden, AppLifecycleState.paused, AppLifecycleState.detached],
          _ => [AppLifecycleState.detached],
        };
    }
    for (final state in path) {
      tester.binding.handleAppLifecycleStateChanged(state);
      await tester.pump();
    }
  }

  Widget embedded(CountingSource source, {Duration poll = const Duration(seconds: 2)}) {
    // The host provides the page: embedded chat renders bare, inside
    // the host's Scaffold, like the preview page and the board drawer.
    return MaterialApp(
      theme: AppTheme.light(),
      home: Scaffold(
        body: ChatScreen(
          source: source,
          embedded: true,
          poll: poll,
          cache: TranscriptCache(),
        ),
      ),
    );
  }

  group('embedded poll visibility switching (item 6)', () {
    testWidgets('switches 2s <-> 15s on lifecycle change, via fake async', (tester) async {
      await goLifecycle(tester, AppLifecycleState.resumed);
      final source = CountingSource();
      await tester.pumpWidget(embedded(source));
      await tester.pumpAndSettle();
      final baseline = source.fetches;
      expect(baseline, greaterThan(0));

      // Visible: 2s cadence — a 4s window holds exactly two ticks,
      // whatever phase the timer was left in by pumpAndSettle.
      await tester.pump(const Duration(seconds: 4));
      await tester.pump();
      expect(source.fetches, baseline + 2);

      // Hidden (paused): the timer restarts at 15s — 14s pass with
      // no tick, the 15th second ticks exactly once.
      await goLifecycle(tester, AppLifecycleState.paused);
      await tester.pump(const Duration(seconds: 14));
      await tester.pump();
      expect(source.fetches, baseline + 2);
      await tester.pump(const Duration(seconds: 1));
      await tester.pump();
      expect(source.fetches, baseline + 3);

      // Back to visible: 2s cadence again.
      await goLifecycle(tester, AppLifecycleState.resumed);
      await tester.pump(const Duration(seconds: 4));
      await tester.pump();
      expect(source.fetches, baseline + 5);

      // Inactive counts as hidden too (not only paused).
      await goLifecycle(tester, AppLifecycleState.inactive);
      await tester.pump(const Duration(seconds: 14));
      await tester.pump();
      expect(source.fetches, baseline + 5);
      await tester.pump(const Duration(seconds: 1));
      await tester.pump();
      expect(source.fetches, baseline + 6);

      await goLifecycle(tester, AppLifecycleState.resumed);
    });

    testWidgets('standalone chat keeps its own poll across lifecycle changes', (tester) async {
      await goLifecycle(tester, AppLifecycleState.resumed);
      final source = CountingSource(key: 'pane:standalone');
      await tester.pumpWidget(MaterialApp(
        theme: AppTheme.light(),
        home: ChatScreen(
          source: source,
          poll: const Duration(seconds: 1),
          cache: TranscriptCache(),
        ),
      ));
      await tester.pumpAndSettle();
      final baseline = source.fetches;

      // The standalone chat never observes the lifecycle: backgrounding
      // must NOT slow it to 15s — 3s still hold three 1s ticks.
      await goLifecycle(tester, AppLifecycleState.paused);
      await tester.pump(const Duration(seconds: 3));
      await tester.pump();
      expect(source.fetches, baseline + 3);

      await goLifecycle(tester, AppLifecycleState.resumed);
    });

    testWidgets('a zero poll stays off when hidden', (tester) async {
      await goLifecycle(tester, AppLifecycleState.resumed);
      final source = CountingSource(key: 'pane:zero');
      await tester.pumpWidget(embedded(source, poll: Duration.zero));
      await tester.pumpAndSettle();
      final baseline = source.fetches;

      await goLifecycle(tester, AppLifecycleState.paused);
      await tester.pump(const Duration(seconds: 30));
      await tester.pump();
      expect(source.fetches, baseline);

      await goLifecycle(tester, AppLifecycleState.resumed);
    });
  });

  group('embedded fullscreen (item 14)', () {
    testWidgets('toggle reaches the fullscreen view and exits back', (tester) async {
      final source = CountingSource(key: 'pane:fullscreen');
      await tester.pumpWidget(embedded(source, poll: Duration.zero));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('chat-fullscreen')), findsOneWidget);
      expect(find.byKey(const Key('chat-fullscreen-exit')), findsNothing);
      expect(find.text('hello back'), findsOneWidget);

      await tester.tap(find.byKey(const Key('chat-fullscreen')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('chat-fullscreen')), findsNothing);
      expect(find.byKey(const Key('chat-fullscreen-exit')), findsOneWidget);
      // Same view, same content — only the chrome changed.
      expect(find.text('hello back'), findsOneWidget);
      expect(find.byKey(const Key('chat-composer')), findsOneWidget);

      await tester.tap(find.byKey(const Key('chat-fullscreen-exit')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('chat-fullscreen')), findsOneWidget);
      expect(find.byKey(const Key('chat-fullscreen-exit')), findsNothing);
      expect(find.text('hello back'), findsOneWidget);
    });
  });

  group('pointer kind and autofocus (item 15)', () {
    test('native pointer kind follows the target platform', () {
      debugDefaultTargetPlatformOverride = TargetPlatform.windows;
      expect(hasCoarsePointer, isFalse);
      debugDefaultTargetPlatformOverride = TargetPlatform.macOS;
      expect(hasCoarsePointer, isFalse);
      debugDefaultTargetPlatformOverride = TargetPlatform.android;
      expect(hasCoarsePointer, isTrue);
      debugDefaultTargetPlatformOverride = TargetPlatform.iOS;
      expect(hasCoarsePointer, isTrue);
      debugDefaultTargetPlatformOverride = TargetPlatform.fuchsia;
      expect(hasCoarsePointer, isTrue);
      debugDefaultTargetPlatformOverride = null;
    });

    testWidgets('desktop autofocuses, mobile-platform does not', (tester) async {
      debugDefaultTargetPlatformOverride = TargetPlatform.windows;
      final desktop = CountingSource(key: 'pane:focus-desktop');
      await tester.pumpWidget(MaterialApp(
        theme: AppTheme.light(),
        home: ChatScreen(source: desktop, poll: Duration.zero, cache: TranscriptCache()),
      ));
      await tester.pumpAndSettle();
      expect(tester.widget<TextField>(find.byKey(const Key('chat-composer'))).autofocus, isTrue);

      debugDefaultTargetPlatformOverride = TargetPlatform.android;
      final mobile = CountingSource(key: 'pane:focus-mobile');
      await tester.pumpWidget(MaterialApp(
        theme: AppTheme.light(),
        home: ChatScreen(source: mobile, poll: Duration.zero, cache: TranscriptCache()),
      ));
      await tester.pumpAndSettle();
      expect(tester.widget<TextField>(find.byKey(const Key('chat-composer'))).autofocus, isFalse);

      // Reset inside the test body: the binding's post-test
      // invariant check runs before the framework tearDown, so a
      // top-level tearDown alone leaves the debug var set.
      debugDefaultTargetPlatformOverride = null;
    });

    testWidgets('embedded chat never autofocuses, even on desktop', (tester) async {
      // Host-context guard, not a device rule: a drawer or grid cell
      // must not steal focus from the page around it.
      debugDefaultTargetPlatformOverride = TargetPlatform.windows;
      await tester.pumpWidget(embedded(CountingSource(key: 'pane:focus-embedded'), poll: Duration.zero));
      await tester.pumpAndSettle();
      expect(tester.widget<TextField>(find.byKey(const Key('chat-composer'))).autofocus, isFalse);
      debugDefaultTargetPlatformOverride = null;
    });
  });
}
