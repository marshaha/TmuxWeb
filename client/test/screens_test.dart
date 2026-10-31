import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/api/api_client.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart';
import 'package:tmuxweb_client/screens/login_screen.dart';
import 'package:tmuxweb_client/theme.dart';

class FakeApi implements Api {
  FakeApi({this.loginError, this.hostsError, this.boardError});

  final ApiException? loginError;
  final ApiException? hostsError;
  final ApiException? boardError;
  final List<String> logins = [];
  final List<String> sent = [];
  final List<String> uploads = [];

  @override
  Future<void> login({required String baseUrl, required String password, String token = ''}) async {
    logins.add('$baseUrl|$password|$token');
    if (loginError != null) throw loginError!;
  }

  @override
  Future<List<Host>> hosts() async {
    if (hostsError != null) throw hostsError!;
    return [
      Host(id: 'localhost', name: 'localhost', kind: 'local', status: 'online', panes: [
        Pane(ref: '%7', title: 'review', command: 'codex', session: 'codex', provider: 'codex', hasTranscript: true),
      ]),
      Host(id: 'abc', name: 'ctyun', kind: 'ssh', status: 'online', panes: [
        Pane(ref: 'abc~%0', title: 'agent', command: 'hermes', session: 'main', provider: 'hermes', hostId: 'abc'),
      ]),
    ];
  }

  @override
  Future<String> interrupt(String paneRef) async => 'Escape';

  @override
  Future<Map<String, String>> paneStates() async => {'%7': 'idle', 'abc~%0': 'busy'};

  @override
  Future<List<RemoteHost>> remoteHosts() async => [
        RemoteHost(id: 'abc', name: 'ctyun', hostname: '100.123.123.8', port: 22,
            username: 'mars', authType: 'password', fingerprint: 'SHA256:abc',
            jumpPath: 'bastion', tmuxPath: '/usr/bin/tmux'),
        RemoteHost(id: 'def', name: 'backup', hostname: '10.0.0.9', port: 22,
            username: 'mars', authType: 'password', enabled: false),
      ];

  final List<Map<String, dynamic>> savedHostPayloads = [];
  final List<List<String>> reorderedIds = [];
  final List<Map<String, dynamic>> testedHostPayloads = [];

  @override
  Future<RemoteHost> createRemoteHost(Map<String, dynamic> payload) async {
    savedHostPayloads.add(Map<String, dynamic>.from(payload));
    return RemoteHost(id: 'new-${payload['name']}', name: '${payload['name']}', hostname: '${payload['hostname']}',
        port: 22, username: '${payload['username']}', authType: 'password');
  }

  @override
  Future<RemoteHost> updateRemoteHost(String id, Map<String, dynamic> payload) async {
    savedHostPayloads.add(Map<String, dynamic>.from(payload));
    return RemoteHost(id: id, name: '${payload['name'] ?? 'x'}', hostname: 'h', port: 22,
        username: 'u', authType: 'password');
  }

  @override
  Future<void> deleteRemoteHost(String id) async {}

  @override
  Future<Map<String, dynamic>> scanHostKey(String hostname, int port) async =>
      {'host_key': '$hostname ssh-ed25519 AAAA', 'fingerprint': 'SHA256:abc'};

  @override
  Future<FileListing> listFiles(String paneRef, {String path = '', bool hidden = false}) async => FileListing(
        path: path.isEmpty ? '/srv/work' : path,
        parent: path.isEmpty ? null : '/srv',
        entries: [
          FileEntry(name: 'notes.md', path: '/srv/work/notes.md', isDir: false, size: 2048),
          FileEntry(name: 'sub', path: '/srv/work/sub', isDir: true),
        ],
      );

  @override
  Future<List<int>> readFile(String paneRef, String path) async => utf8.encode('# 标题\n\n正文');

  @override
  Future<void> uploadFile(String paneRef, String dir, String name, List<int> bytes) async {
    uploads.add('$dir/$name|${bytes.length}');
  }

  @override
  Future<List<int>> zipFiles(String paneRef, List<String> paths, String base) async => const [80, 75, 3, 4];

  @override
  Future<Transcript> transcript(String paneRef, {int limit = 200, bool refresh = false}) async =>
      Transcript(mode: 'transcript', provider: 'codex', mtime: 1, messages: [
        ChatMessage(id: 'm1', role: 'assistant', text: '来自 $paneRef 的回复'),
      ]);

  @override
  Future<void> sendInput(String paneRef, String text, {bool submit = true}) async => sent.add(text);

  @override
  Future<ShareMeta> shareMeta(String slug) async =>
      ShareMeta(slug: slug, title: '共享会话', mode: 'transcript', allowInput: false);

  @override
  Future<void> shareUnlock(String slug, String password) async {}

  @override
  Future<Transcript> shareTranscript(String slug, {int limit = 300}) async =>
      Transcript(mode: 'transcript', mtime: 5, readOnly: true, messages: [
        ChatMessage(id: 's1', role: 'assistant', text: '只读历史'),
      ]);

  @override
  Future<void> shareInput(String slug, String text, {bool submit = true}) async => throw ApiException(403, '该会话只读');

  // Web parity batch 2: a small board so the board screen can be tested.
  final List<String> addedFavorites = [];
  final List<String> unfavorited = [];
  final List<String> renamedFavorites = [];
  final List<String> deletedRecent = [];
  final List<String> deletedCards = [];

  @override
  Future<Map<String, dynamic>> homeBoard() async {
    if (boardError != null) throw boardError!;
    return {
        'favorites': [
          {
            'pane': '%7',
            'note': '重点看',
            'status': 'idle',
            'provider': 'codex',
            'title': 'review',
            'default_title': 'review',
            'custom_title': false,
            'pane_title': 'review',
            'path': '/srv/work',
            'session': 'codex',
            'window': 'main',
            'host': 'localhost',
            'command': 'codex',
            'preview': {'role': 'assistant', 'text': '已完成第一版'},
            'waiting': false,
          },
        ],
        'recent': [
          {
            'pane': 'abc~%0',
            'visited_at': 1761900000,
            'title': 'agent',
            'host': 'ctyun',
            'session': 'main',
            'window': 'main',
            'provider': 'hermes',
            'status': 'busy',
            'online': true,
          },
        ],
        'cards': [
          {'id': 'c1', 'title': '结论', 'body': '先做 API 再做界面', 'pane_id': '%7', 'kind': 'note', 'created_at': 1761900000},
        ],
        'stats': {'panes': 2, 'busy': 1, 'idle': 1, 'favorites': 1, 'cards': 1},
      };
  }

  /// The favorite set the hosts tree reads; add/delete keep it in sync,
  /// like the server, so star toggles are observable in widget tests.
  final Set<String> favoritePanes = {'abc~%0'};

  @override
  Future<Map<String, dynamic>> homeFavorites() async => {'favorites': favoritePanes.toList(), 'notes': {}};

  @override
  Future<void> addHomeFavorite(String paneRef, {String note = '', String title = ''}) async {
    addedFavorites.add(paneRef);
    favoritePanes.add(paneRef);
  }

  @override
  Future<void> renameHomeFavorite(String paneRef, String title) async {
    renamedFavorites.add('$paneRef|$title');
  }

  @override
  Future<void> deleteHomeFavorite(String paneRef) async {
    unfavorited.add(paneRef);
    favoritePanes.remove(paneRef);
  }

  @override
  Future<void> touchHomeRecent(String paneRef) async {}

  @override
  Future<void> deleteHomeRecent(String paneRef) async {
    deletedRecent.add(paneRef);
  }

  @override
  Future<Map<String, dynamic>> homeSearch(String query, {int limit = 20, bool messages = true}) async {
    // Web's two-phase search: names first, conversations second.
    final names = {
      'query': query,
      'mode': 'literal',
      'hosts': [
        {'id': 'localhost', 'name': 'localhost', 'hostname': 'localhost', 'panes': 1},
      ],
      'sessions': [
        {'host': 'localhost', 'host_id': 'localhost', 'session': 'codex', 'panes': 1},
      ],
      'panes': [
        {'id': '%7', 'host_id': 'localhost', 'title': 'review', 'session': 'codex', 'provider': 'codex'},
      ],
      'messages': [],
      'cards': [],
      'messages_pending': true,
      'messages_partial': true,
    };
    if (!messages) return names;
    return {
      ...names,
      'messages': [
        {'pane': '%7', 'title': 'review', 'session': 'codex', 'host': 'localhost', 'role': 'assistant', 'snippet': '已完成第一版'},
      ],
      'cards': [
        {'id': 'c1', 'title': '结论', 'body': '先做 API 再做界面', 'kind': 'note'},
      ],
      'messages_pending': false,
      'messages_partial': false,
    };
  }

  @override
  Future<List<Map<String, dynamic>>> homeCards() async => const [];

  @override
  Future<Map<String, dynamic>> createHomeCard(
          {required String title, String body = '', String paneRef = '', String kind = 'note'}) async =>
      {'title': title};

  @override
  Future<void> deleteHomeCard(String cardId) async {
    deletedCards.add(cardId);
  }

  String? updatedTheme;
  bool loggedOut = false;
  final Map<String, bool> pluginStates = {'cross-server-relay': true, 'broken-plugin': false};
  final List<String> pluginToggles = [];

  @override
  Future<Map<String, dynamic>> prefs() async => const {'prefs': {}};

  @override
  Future<Map<String, dynamic>> updateTheme(String theme) async {
    updatedTheme = theme;
    return {'prefs': {'theme': theme}};
  }

  @override
  Future<Map<String, dynamic>> settingsInfo() async =>
      const {'password_source': 'file', 'hosts': 2, 'api_token': false};

  @override
  Future<void> changePassword(String current, String next) async {
    if (current != 'old') throw ApiException(403, '当前密码不正确');
    if (next.length < 6) throw ApiException(400, '新密码至少 6 位');
  }

  @override
  Future<Map<String, dynamic>> settingsExport() async => {
        'app': 'TmuxWeb',
        'exported_at': '2026-10-31T00:00:00',
        'hosts': [
          {'name': 'ctyun', 'hostname': '100.123.123.8', 'port': 22, 'username': 'mars', 'credential': 'secret'},
          {'name': 'backup', 'hostname': '10.0.0.9', 'port': 22, 'username': 'mars', 'credential': ''},
        ],
        'board': {'favorites': []},
      };

  @override
  Future<void> logout() async {
    loggedOut = true;
  }

  @override
  Future<List<Map<String, dynamic>>> plugins() async => [
        {
          'name': 'cross-server-relay',
          'version': '1.0.0',
          'description': '跨服务器中继',
          'enabled': pluginStates['cross-server-relay'] ?? false,
          'loaded': true,
          'error': '',
        },
        {
          'name': 'broken-plugin',
          'version': '0.1.0',
          'description': '',
          'enabled': pluginStates['broken-plugin'] ?? false,
          'loaded': false,
          'error': '加载失败：入口模块不存在',
        },
      ];

  @override
  Future<void> setPluginEnabled(String name, bool enabled) async {
    pluginToggles.add('$name=$enabled');
    pluginStates[name] = enabled;
  }

  @override
  Future<List<Map<String, dynamic>>> listShares() async => const [];

  @override
  Future<Map<String, dynamic>> createShare(
          {required String paneRef, required String slug, String password = '', int expiresIn = 86400, bool allowInput = true}) async =>
      {'slug': slug};

  @override
  Future<Map<String, dynamic>> extendShare(String shareId, int seconds) async => const {};

  @override
  Future<void> stopShare(String shareId) async {}

  @override
  Future<void> deleteShare(String shareId) async {}

  @override
  Future<void> resetSharePassword(String shareId, String password) async {}

  @override
  Future<Map<String, dynamic>> createSession({String host = 'localhost', required String name, String path = ''}) async =>
      {'session': name};

  @override
  Future<List<Map<String, dynamic>>> reorderRemoteHosts(List<String> ids) async {
    reorderedIds.add(List<String>.from(ids));
    return const [];
  }

  @override
  Future<Map<String, dynamic>> testRemoteHost(Map<String, dynamic> payload) async {
    testedHostPayloads.add(Map<String, dynamic>.from(payload));
    return const {'ssh_ok': true, 'tmux_ok': true, 'pane_count': 3, 'sessions': ['main', 'codex']};
  }

  @override
  Future<List<Map<String, dynamic>>> listPanes() async => const [];

  @override
  Future<String> capturePane(String paneRef, {int history = 500}) async => '';

  @override
  Future<void> sendPaneKey(String paneRef, String key) async {}
}

Widget wrap(Widget child) => MaterialApp(theme: AppTheme.light(), home: child);

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('an empty password never calls the API', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(LoginScreen(apiFactory: (url) => api)));
    await tester.tap(find.byKey(const Key('login-submit')));
    await tester.pump();

    expect(find.text('请填写密码或 API Token'), findsOneWidget);
    expect(api.logins, isEmpty);
  });

  testWidgets('the password is masked until the eye is tapped', (tester) async {
    await tester.pumpWidget(wrap(LoginScreen(apiFactory: (url) => FakeApi())));
    TextField field() => tester.widget<TextField>(find.byKey(const Key('login-password')));

    expect(field().obscureText, isTrue);
    await tester.tap(find.byTooltip('显示密码'));
    await tester.pump();
    expect(field().obscureText, isFalse);
  });

  testWidgets('a rejected login keeps the user on the form', (tester) async {
    await tester.pumpWidget(wrap(LoginScreen(
      initialBaseUrl: 'chat.local:8899',
      apiFactory: (url) => FakeApi(loginError: ApiException(401, '密码错误')),
    )));
    await tester.enterText(find.byKey(const Key('login-password')), 'bad');
    await tester.tap(find.byKey(const Key('login-submit')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('login-error')), findsOneWidget);
    expect(find.text('密码错误'), findsOneWidget);
  });

  testWidgets('a successful login opens the host list and remembers the URL', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(LoginScreen(initialBaseUrl: '10.168.236.31:8899/', apiFactory: (url) => api)));
    await tester.enterText(find.byKey(const Key('login-password')), 'pw');
    await tester.tap(find.byKey(const Key('login-submit')));
    await tester.pumpAndSettle();

    expect(api.logins.single, 'http://10.168.236.31:8899|pw|');
    expect(find.text('localhost'), findsOneWidget);
    expect(find.text('ctyun'), findsOneWidget);
    expect(find.text('2 台主机 · 2 个 pane'), findsOneWidget);
    expect((await SharedPreferences.getInstance()).getString('tmuxweb.baseUrl'), 'http://10.168.236.31:8899');
  });

  testWidgets('the last server URL is restored', (tester) async {
    SharedPreferences.setMockInitialValues({'tmuxweb.baseUrl': 'http://robo:8899'});
    await tester.pumpWidget(wrap(LoginScreen(apiFactory: (url) => FakeApi())));
    await tester.pumpAndSettle();

    expect(tester.widget<TextField>(find.byKey(const Key('login-base-url'))).controller!.text, 'http://robo:8899');
  });

  testWidgets('an API token login works without a password', (tester) async {
    final api = FakeApi();
    await tester.pumpWidget(wrap(LoginScreen(initialBaseUrl: 'chat.local:8899', apiFactory: (url) => api)));
    await tester.tap(find.text('改用 API Token'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('login-token')), 'tok-1');
    await tester.tap(find.byKey(const Key('login-submit')));
    await tester.pumpAndSettle();

    expect(api.logins.single, 'http://chat.local:8899||tok-1');
  });

  testWidgets('hosts screen lists remote panes and opens a chat', (tester) async {
    await tester.pumpWidget(wrap(HostsScreen(api: FakeApi(), baseUrl: 'http://chat.local:8899')));
    await tester.pumpAndSettle();

    expect(find.textContaining('abc~%0'), findsOneWidget);
    await tester.tap(find.byKey(const Key('pane-abc~%0')));
    await tester.pumpAndSettle();
    expect(find.text('来自 abc~%0 的回复'), findsOneWidget);
  });

  testWidgets('hosts screen reports a failing API with a retry', (tester) async {
    await tester.pumpWidget(wrap(HostsScreen(
      api: FakeApi(hostsError: ApiException(401, '登录已失效')),
      baseUrl: 'http://chat.local:8899',
    )));
    await tester.pumpAndSettle();

    expect(find.textContaining('登录已失效'), findsOneWidget);
    expect(find.text('重试'), findsOneWidget);
  });

  testWidgets('a read-only share opens without a composer', (tester) async {
    await tester.pumpWidget(wrap(ShareScreen(api: FakeApi(), slug: 'frozen')));
    await tester.pumpAndSettle();

    expect(find.text('只读历史'), findsOneWidget);
    expect(find.byKey(const Key('read-only-banner')), findsOneWidget);
    expect(find.byKey(const Key('chat-composer')), findsNothing);
    expect(find.text('只读'), findsOneWidget);
  });
}
