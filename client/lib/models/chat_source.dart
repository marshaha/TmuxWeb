/// Chat data sources: a pane (authenticated) or a public share link.
library;

import 'dart:async';

import '../api/api_client.dart';
import '../services/screen_stream.dart';
import 'models.dart';

abstract class ChatSource {
  /// Stable identity used by the transcript cache.
  String get cacheKey;

  String get title;

  String get subtitle;

  bool get readOnly;

  Future<Transcript> fetch({bool refresh = false});

  /// Live capture frames for screen-mode panes. The default is an
  /// empty stream (polling stays in charge); pane sources override
  /// it with the `/ws/panes/{id}` socket.
  Stream<String> screenStream() => const Stream.empty();

  /// Reconnect state of the live stream behind [screenStream], for
  /// ChatScreen's `重连中…` badge. Empty / false for sources without a
  /// socket; pane sources forward their ScreenStream's state. The
  /// stream only emits on change, so readers also check
  /// [isScreenReconnecting] when subscribing.
  Stream<bool> screenReconnecting() => const Stream.empty();

  bool get isScreenReconnecting => false;

  /// Pane-backed context for chat extras (path links, attachments,
  /// `#N` titles). Null / empty for share sources, which have no pane
  /// API: those surfaces keep the old rendering with no path links
  /// and no attach button.
  Api? get chatApi => null;

  String? get paneRef => null;

  /// The pane's working directory, used as the base for the
  /// `upload_files` attachment directory.
  String get panePath => '';

  /// Web's `#N` short-number prefix, or null when the pane has none.
  /// Display only — it never changes MCP relay / server addressing.
  String? get numberLabel => null;

  Future<void> send(String text, {bool submit = true});

  /// Interrupt the running turn; returns the key that was sent.
  Future<String> stop();
}

class PaneChatSource implements ChatSource {
  PaneChatSource({required this.api, required this.pane});

  final Api api;
  final Pane pane;

  /// The ScreenStream behind the current [screenStream] wrapper, so
  /// [screenReconnecting] reports the same socket's state.
  ScreenStream? _screenStream;

  @override
  Api get chatApi => api;

  @override
  String get paneRef => pane.ref;

  @override
  String get panePath => pane.path;

  @override
  String? get numberLabel => pane.numberLabel.isEmpty ? null : pane.numberLabel;

  @override
  String get cacheKey => 'pane:${pane.ref}';

  @override
  String get title => pane.label;

  @override
  String get subtitle => [
        if (pane.provider != null) pane.provider!,
        if (pane.session.isNotEmpty) pane.session,
        if (pane.path.isNotEmpty) pane.path,
      ].join(' · ');

  @override
  bool get readOnly => false;

  @override
  Future<Transcript> fetch({bool refresh = false}) {
    // Panes without a transcript capability (plain shells) have no agent
    // transcript: the server answers 409. Web shows those panes as a
    // screen view, so return a screen-mode transcript and let the chat
    // screen start its capture stream instead of polling into a 409.
    if (!pane.hasTranscript) return Future.value(Transcript(mode: 'screen'));
    return api.transcript(pane.ref, refresh: refresh);
  }

  @override
  Stream<String> screenStream() {
    final socketApi = api is TerminalApi ? api as TerminalApi : null;
    if (socketApi == null) return const Stream.empty();
    final inner = ScreenStream(
      uri: socketApi.wsUri('/ws/panes/${encodePaneRef(pane.ref)}'),
      headers: socketApi.wsHeaders,
    );
    _screenStream = inner;
    // Cancelling the returned stream must stop the socket and its
    // reconnect timer, not just drop frames on the floor.
    final controller = StreamController<String>(onCancel: () => inner.close());
    inner.stream.listen(controller.add,
        onDone: controller.close, onError: controller.addError);
    inner.start();
    return controller.stream;
  }

  @override
  Stream<bool> screenReconnecting() =>
      _screenStream?.reconnecting ?? const Stream.empty();

  @override
  bool get isScreenReconnecting => _screenStream?.isReconnecting ?? false;

  @override
  Future<void> send(String text, {bool submit = true}) =>
      api.sendInput(pane.ref, text, submit: submit);

  @override
  Future<String> stop() => api.interrupt(pane.ref);
}

class ShareChatSource implements ChatSource {
  ShareChatSource({required this.api, required this.meta});

  final Api api;
  final ShareMeta meta;

  // No pane socket / pane API on shares: the ChatSource defaults
  // (empty stream, no pane context) apply; spelled out because
  // `implements` does not inherit the abstract class's defaults.
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
  String get cacheKey => 'share:${meta.slug}';

  @override
  String get title => meta.title;

  @override
  String get subtitle => '分享 · ${meta.slug}';

  @override
  bool get readOnly => !meta.allowInput;

  @override
  Future<Transcript> fetch({bool refresh = false}) =>
      api.shareTranscript(meta.slug);

  @override
  Stream<String> screenStream() => const Stream.empty();

  @override
  Future<void> send(String text, {bool submit = true}) =>
      api.shareInput(meta.slug, text, submit: submit);

  /// A share link has no stop control: visitors cannot interrupt the agent.
  @override
  Future<String> stop() async => throw UnsupportedError('无法在分享页面停止');
}
