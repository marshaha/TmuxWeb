import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/models/chat_source.dart';
import 'package:tmuxweb_client/models/models.dart';

import 'screens_test.dart' show FakeApi;

void main() {
  test('pane without transcript capability fetches a screen transcript, not the API', () async {
    // FakeApi.transcript would return a transcript-mode transcript, so a
    // screen result here proves fetch never called the API (the server
    // answers 409 for such panes).
    final source = PaneChatSource(
      api: FakeApi(),
      pane: Pane(ref: '%1', title: 'shell', command: 'bash', session: 'demo-shell'),
    );
    final transcript = await source.fetch();
    expect(transcript.isScreen, isTrue);
  });

  test('pane with transcript capability still fetches from the API', () async {
    final source = PaneChatSource(
      api: FakeApi(),
      pane: Pane(ref: '%7', title: 'review', command: 'codex', session: 'codex', hasTranscript: true),
    );
    final transcript = await source.fetch();
    expect(transcript.mode, 'transcript');
    expect(transcript.provider, 'codex');
  });

  test('pane source exposes its number label, ref and path for chat extras', () {
    final api = FakeApi();
    final pane = Pane(ref: '%7', title: 'review', command: 'codex', session: 'codex', path: '/srv/work', hasTranscript: true, number: 7);
    final source = PaneChatSource(api: api, pane: pane);
    expect(source.numberLabel, '#7');
    expect(source.paneRef, '%7');
    expect(source.panePath, '/srv/work');
    expect(source.chatApi, same(api));
    expect(source.isScreenReconnecting, isFalse);

    final unnumbered = PaneChatSource(
      api: api,
      pane: Pane(ref: '%8', title: 'shell', command: 'bash', session: 'demo'),
    );
    expect(unnumbered.numberLabel, isNull);
  });

  test('share source has no pane context or reconnect state', () async {
    final source = ShareChatSource(
      api: FakeApi(),
      meta: ShareMeta(slug: 's1', title: '共享会话', mode: 'transcript', allowInput: false),
    );
    expect(source.chatApi, isNull);
    expect(source.paneRef, isNull);
    expect(source.panePath, '');
    expect(source.numberLabel, isNull);
    expect(source.isScreenReconnecting, isFalse);
    expect(await source.screenReconnecting().isEmpty, isTrue);
  });
}
