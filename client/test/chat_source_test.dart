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
}
