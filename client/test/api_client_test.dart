import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:tmuxweb_client/api/api_client.dart';

void main() {
  group('normalizeBaseUrl', () {
    test('adds a scheme and trims trailing slashes', () {
      expect(normalizeBaseUrl('10.168.236.31:8899/'), 'http://10.168.236.31:8899');
      expect(normalizeBaseUrl(' https://chat.example.com// '), 'https://chat.example.com');
    });

    test('rejects empty or hostless values', () {
      expect(() => normalizeBaseUrl('  '), throwsA(isA<ApiException>()));
      expect(() => normalizeBaseUrl('http://'), throwsA(isA<ApiException>()));
    });
  });

  test('encodePaneRef escapes the percent in pane references', () {
    expect(encodePaneRef('%7'), '%257');
    expect(encodePaneRef('5ca191677f95a36d~%14'), '5ca191677f95a36d~%2514');
  });

  group('ApiClient', () {
    late List<http.Request> seen;

    MockClient server(Map<String, Object> routes) => MockClient((request) async {
          seen.add(request);
          final key = '${request.method} ${request.url.path}';
          final match = routes.entries.where((entry) => key == entry.key).toList();
          if (match.isEmpty) return http.Response('{"detail":"not found"}', 404);
          final value = match.first.value;
          return switch (value) {
            http.Response response => response,
            String body => http.Response(body, 200, headers: {'content-type': 'application/json'}),
            _ => http.Response(jsonEncode(value), 200, headers: {'content-type': 'application/json'}),
          };
        });

    setUp(() => seen = []);

    test('login stores the session cookie from a 303 response', () async {
      final client = ApiClient(
        httpClient: server({
          'POST /login': http.Response('', 303, headers: {
            'set-cookie': 'tmux_web_session=abc123; Path=/; HttpOnly; SameSite=Strict',
            'location': '/',
          }),
        }),
      );
      await client.login(baseUrl: 'chat.local:8899', password: 'secret');
      expect(client.cookies['tmux_web_session'], 'abc123');
      expect(client.baseUrl, 'http://chat.local:8899');
      expect(seen.single.body, 'password=secret');
      expect(seen.single.followRedirects, isFalse);
    });

    test('login surfaces a wrong password', () async {
      final client = ApiClient(
        httpClient: server({'POST /login': http.Response('{"detail":"invalid password"}', 401)}),
      );
      await expectLater(
        client.login(baseUrl: 'http://chat.local:8899', password: 'nope'),
        throwsA(isA<ApiException>().having((error) => error.status, 'status', 401)),
      );
    });

    test('login with a token verifies it against /api/hosts', () async {
      final client = ApiClient(httpClient: server({'GET /api/hosts': '{"hosts":[]}'}));
      await client.login(baseUrl: 'http://chat.local:8899', password: '', token: 'tok-1');
      expect(seen.single.headers['Authorization'], 'Bearer tok-1');
    });

    test('login without any credential never hits the network', () async {
      final client = ApiClient(httpClient: server({}));
      await expectLater(
        client.login(baseUrl: 'http://chat.local:8899', password: ''),
        throwsA(isA<ApiException>()),
      );
      expect(seen, isEmpty);
    });

    test('hosts and panes are parsed with provider metadata', () async {
      final client = ApiClient(
        httpClient: server({
          'GET /api/hosts': jsonEncode({
            'hosts': [
              {
                'id': 'localhost',
                'name': 'localhost',
                'kind': 'local',
                'status': 'online',
                'panes': [
                  {
                    'id': '%7', 'title': 'review', 'command': 'codex', 'session': 'codex', 'provider': 'codex',
                    'capabilities': {'transcript': true, 'terminal': true, 'relay': true, 'share': true},
                  },
                  {
                    'id': '%8', 'title': 'zsh', 'command': 'zsh', 'session': 'shell', 'provider': null,
                    'capabilities': {'transcript': false},
                  },
                ],
              },
              {
                'id': 'abc123', 'name': 'ctyun', 'kind': 'ssh', 'status': 'online',
                'panes': [
                  {'id': 'abc123~%0', 'title': 'agent', 'command': 'hermes', 'provider': 'hermes', 'host_id': 'abc123'},
                ],
              },
            ],
          }),
        }),
      );
      final hosts = await client.hosts();
      expect(hosts, hasLength(2));
      expect(hosts.first.panes.first.provider, 'codex');
      expect(hosts.first.panes.first.hasTranscript, isTrue);
      expect(hosts.first.panes[1].provider, isNull);
      expect(hosts.first.panes[1].hasTranscript, isFalse);
      expect(hosts.last.panes.single.ref, 'abc123~%0');
      expect(hosts.last.panes.single.hostId, 'abc123');
    });

    test('transcript parses messages and mtime, and encodes the pane ref', () async {
      final client = ApiClient(
        httpClient: server({
          'GET /api/panes/%257/transcript': jsonEncode({
            'provider': 'codex',
            'session_id': 's-1',
            'mtime': 1793023314954331211,
            'messages': [
              {'id': 'm1', 'role': 'user', 'text': '你好', 'timestamp': '2026-10-26T14:01:37.254Z'},
              {'id': 'm2', 'role': 'assistant', 'text': '收到'},
            ],
          }),
        }),
      );
      final transcript = await client.transcript('%7', limit: 50);
      expect(transcript.provider, 'codex');
      expect(transcript.mtime, 1793023314954331211);
      expect(transcript.messages.map((message) => message.role), ['user', 'assistant']);
      expect(seen.single.url.path, '/api/panes/%257/transcript');
      expect(seen.single.url.queryParameters['limit'], '50');
    });

    test('a forced refresh asks the server to re-locate the session file', () async {
      final client = ApiClient(
        httpClient: server({'GET /api/panes/%257/transcript': '{"mode":"transcript","messages":[],"mtime":1}'}),
      );
      await client.transcript('%7', refresh: true);
      expect(seen.single.url.queryParameters['refresh'], '1');
      await client.transcript('%7');
      expect(seen.last.url.queryParameters.containsKey('refresh'), isFalse);
    });

    test('screen-mode transcripts keep the raw terminal text', () async {
      final client = ApiClient(
        httpClient: server({
          'GET /api/panes/%258/transcript': jsonEncode({
            'mode': 'screen', 'provider': null, 'session_id': null, 'mtime': 'sha1', 'messages': [], 'content': r'zsh$ ls',
          }),
        }),
      );
      final transcript = await client.transcript('%8');
      expect(transcript.isScreen, isTrue);
      expect(transcript.content, r'zsh$ ls');
    });

    test('sendInput posts text and submit', () async {
      final client = ApiClient(httpClient: server({'POST /api/panes/%257/input': '{"status":"sent"}'}));
      await client.sendInput('%7', '继续', submit: false);
      expect(jsonDecode(seen.single.body), {'text': '继续', 'submit': false});
    });

    test('a read-only share reports allow_input false and rejects input', () async {
      final client = ApiClient(
        httpClient: server({
          'GET /share/frozen/api/meta': jsonEncode({
            'slug': 'frozen', 'title': '共享会话', 'mode': 'transcript', 'allow_input': false,
            'provider': 'codex', 'password_protected': false, 'expires_at': 1,
          }),
          'GET /share/frozen/api/transcript': jsonEncode({'mode': 'transcript', 'mtime': 3, 'messages': []}),
          'POST /share/frozen/api/input': http.Response('{"detail":"this share is read-only"}', 403),
        }),
      );
      final meta = await client.shareMeta('frozen');
      expect(meta.allowInput, isFalse);
      final transcript = await client.shareTranscript('frozen');
      expect(transcript.readOnly, isTrue);
      await expectLater(
        client.shareInput('frozen', 'hi'),
        throwsA(isA<ApiException>()
            .having((error) => error.status, 'status', 403)
            .having((error) => error.isReadOnly, 'isReadOnly', isTrue)),
      );
    });

    test('shareUnlock stores the per-share cookie', () async {
      final client = ApiClient(
        httpClient: server({
          'POST /share/guarded/login': http.Response('', 303, headers: {
            'set-cookie': 'share_guarded=xyz; Path=/share/guarded; HttpOnly',
          }),
        }),
      );
      await client.shareUnlock('guarded', 'pw');
      expect(client.cookies['share_guarded'], 'xyz');
    });

    test('errors keep the server detail message', () async {
      final client = ApiClient(
        httpClient: server({'GET /api/panes/%259/transcript': http.Response('{"detail":"no transcript"}', 409)}),
      );
      await expectLater(
        client.transcript('%9'),
        throwsA(isA<ApiException>()
            .having((error) => error.status, 'status', 409)
            .having((error) => error.message, 'message', 'no transcript')),
      );
    });

    test('an unreachable server becomes a readable error', () async {
      final client = ApiClient(httpClient: MockClient((_) async => throw const _Unreachable()));
      await expectLater(
        client.login(baseUrl: 'http://10.0.0.1:8899', password: 'pw'),
        throwsA(isA<ApiException>().having((error) => error.message, 'message', contains('无法连接'))),
      );
    });
  });
}

class _Unreachable implements Exception {
  const _Unreachable();

  @override
  String toString() => 'SocketException: Connection refused';
}
