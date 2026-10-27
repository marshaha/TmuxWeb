import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/api/api_client.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/screens/hosts_screen.dart';
import 'package:tmuxweb_client/screens/login_screen.dart';
import 'package:tmuxweb_client/theme.dart';

class FakeApi implements Api {
  FakeApi({this.loginError, this.hostsError});

  final ApiException? loginError;
  final ApiException? hostsError;
  final List<String> logins = [];
  final List<String> sent = [];

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
  Future<Transcript> transcript(String paneRef, {int limit = 200}) async =>
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
