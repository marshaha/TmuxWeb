/// Chat data sources: a pane (authenticated) or a public share link.
library;

import '../api/api_client.dart';
import 'models.dart';

abstract class ChatSource {
  /// Stable identity used by the transcript cache.
  String get cacheKey;

  String get title;

  String get subtitle;

  bool get readOnly;

  Future<Transcript> fetch({bool refresh = false});

  Future<void> send(String text, {bool submit = true});

  /// Interrupt the running turn; returns the key that was sent.
  Future<String> stop();
}

class PaneChatSource implements ChatSource {
  PaneChatSource({required this.api, required this.pane});

  final Api api;
  final Pane pane;

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
  Future<Transcript> fetch({bool refresh = false}) => api.transcript(pane.ref, refresh: refresh);

  @override
  Future<void> send(String text, {bool submit = true}) => api.sendInput(pane.ref, text, submit: submit);

  @override
  Future<String> stop() => api.interrupt(pane.ref);
}

class ShareChatSource implements ChatSource {
  ShareChatSource({required this.api, required this.meta});

  final Api api;
  final ShareMeta meta;

  @override
  String get cacheKey => 'share:${meta.slug}';

  @override
  String get title => meta.title;

  @override
  String get subtitle => '分享 · ${meta.slug}';

  @override
  bool get readOnly => !meta.allowInput;

  @override
  Future<Transcript> fetch({bool refresh = false}) => api.shareTranscript(meta.slug);

  @override
  Future<void> send(String text, {bool submit = true}) => api.shareInput(meta.slug, text, submit: submit);

  /// A share link has no stop control: visitors cannot interrupt the agent.
  @override
  Future<String> stop() async => throw UnsupportedError('无法在分享页面停止');
}
