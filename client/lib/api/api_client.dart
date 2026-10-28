/// Native API client: cookie session (password login) or bearer token.
library;

import 'dart:convert';

import 'package:http/http.dart' as http;

import '../models/models.dart';

class ApiException implements Exception {
  ApiException(this.status, this.message);

  final int status;
  final String message;

  bool get isUnauthorized => status == 401;
  bool get isReadOnly => status == 403;
  bool get isNoTranscript => status == 409;

  @override
  String toString() => message;
}

/// Everything the screens need; a fake implementation backs the widget tests.
abstract class Api {
  Future<void> login({required String baseUrl, required String password, String token = ''});

  Future<List<Host>> hosts();

  Future<Transcript> transcript(String paneRef, {int limit = 200, bool refresh = false});

  Future<void> sendInput(String paneRef, String text, {bool submit = true});

  /// Stop the running turn; returns the key that was sent (C-q or Escape).
  Future<String> interrupt(String paneRef);

  Future<ShareMeta> shareMeta(String slug);

  Future<void> shareUnlock(String slug, String password);

  Future<Transcript> shareTranscript(String slug, {int limit = 300});

  Future<void> shareInput(String slug, String text, {bool submit = true});

  /// Busy/idle state per pane reference (empty when the probe is unavailable).
  Future<Map<String, String>> paneStates();

  Future<List<RemoteHost>> remoteHosts();

  Future<RemoteHost> createRemoteHost(Map<String, dynamic> payload);

  Future<RemoteHost> updateRemoteHost(String id, Map<String, dynamic> payload);

  Future<void> deleteRemoteHost(String id);

  /// Reads the SSH host key so a new host can be pinned on first contact.
  Future<Map<String, dynamic>> scanHostKey(String hostname, int port);

  Future<FileListing> listFiles(String paneRef, {String path = '', bool hidden = false});

  /// Raw bytes of a file, served inline (used for previews and downloads).
  Future<List<int>> readFile(String paneRef, String path);

  Future<void> uploadFile(String paneRef, String dir, String name, List<int> bytes);

  /// Zip of the given paths; an empty list zips the directory `base`.
  Future<List<int>> zipFiles(String paneRef, List<String> paths, String base);
}

String normalizeBaseUrl(String raw) {
  var value = raw.trim();
  if (value.isEmpty) throw ApiException(0, '请填写服务器地址');
  if (!value.startsWith('http://') && !value.startsWith('https://')) value = 'http://$value';
  while (value.endsWith('/')) {
    value = value.substring(0, value.length - 1);
  }
  final uri = Uri.tryParse(value);
  if (uri == null || uri.host.isEmpty || uri.host.contains(' ')) throw ApiException(0, '服务器地址无效：$raw');
  return value;
}

/// Pane references carry `%` (`%7`, `host~%7`), which must be percent-encoded.
String encodePaneRef(String ref) => Uri.encodeComponent(ref);

/// Surface a terminal view needs: socket endpoint and handshake headers.
abstract class TerminalApi {
  Map<String, String> get wsHeaders;

  Uri wsUri(String path, [Map<String, String>? query]);
}

class ApiClient implements Api, TerminalApi {
  ApiClient({String baseUrl = '', http.Client? httpClient})
      : baseUrl = baseUrl.isEmpty ? '' : normalizeBaseUrl(baseUrl),
        _http = httpClient ?? http.Client();

  String baseUrl;
  final http.Client _http;
  final Map<String, String> cookies = {};
  String token = '';

  Uri _uri(String path, [Map<String, String>? query]) {
    final uri = Uri.parse('$baseUrl$path');
    return query == null ? uri : uri.replace(queryParameters: query);
  }

  Map<String, String> _headers({bool json = false}) {
    final headers = <String, String>{'Accept': 'application/json'};
    if (json) headers['Content-Type'] = 'application/json';
    if (token.isNotEmpty) headers['Authorization'] = 'Bearer $token';
    if (cookies.isNotEmpty) {
      headers['Cookie'] = cookies.entries.map((entry) => '${entry.key}=${entry.value}').join('; ');
    }
    return headers;
  }

  void _storeCookies(http.BaseResponse response) {
    final raw = response.headers['set-cookie'];
    if (raw == null || raw.isEmpty) return;
    for (final part in raw.split(RegExp(r',(?=[^;=]+=)'))) {
      final pair = part.split(';').first.trim();
      final index = pair.indexOf('=');
      if (index <= 0) continue;
      final name = pair.substring(0, index).trim();
      final value = pair.substring(index + 1).trim();
      if (value.isEmpty) {
        cookies.remove(name);
      } else {
        cookies[name] = value;
      }
    }
  }

  Never _fail(http.Response response) {
    var detail = '';
    try {
      final decoded = jsonDecode(utf8.decode(response.bodyBytes));
      if (decoded is Map && decoded['detail'] != null) detail = '${decoded['detail']}';
    } catch (_) {}
    final message = switch (response.statusCode) {
      401 => detail.isEmpty ? '登录已失效，请重新登录' : detail,
      403 => detail.isEmpty ? '该会话只读，不能发送消息' : detail,
      404 => detail.isEmpty ? '会话或分享不存在' : detail,
      409 => detail.isEmpty ? '这个会话没有结构化记录' : detail,
      422 => detail.isEmpty ? '请求参数不合法' : detail,
      503 => detail.isEmpty ? '服务暂时不可用' : detail,
      _ => detail.isEmpty ? 'HTTP ${response.statusCode}' : detail,
    };
    throw ApiException(response.statusCode, message);
  }

  Map<String, dynamic> _decode(http.Response response) {
    if (response.statusCode >= 400) _fail(response);
    if (response.body.isEmpty) return const {};
    final decoded = jsonDecode(utf8.decode(response.bodyBytes));
    return decoded is Map<String, dynamic> ? decoded : <String, dynamic>{'value': decoded};
  }

  @override
  Future<void> login({required String baseUrl, required String password, String token = ''}) async {
    this.baseUrl = normalizeBaseUrl(baseUrl);
    this.token = token.trim();
    if (this.token.isNotEmpty) {
      await hosts(); // verifies the token by fetching a cheap endpoint
      return;
    }
    if (password.isEmpty) throw ApiException(0, '请填写密码或 API Token');
    final request = http.Request('POST', _uri('/login'))
      ..followRedirects = false
      ..headers['Content-Type'] = 'application/x-www-form-urlencoded'
      ..body = Uri(queryParameters: {'password': password}).query;
    final http.Response response;
    try {
      response = await http.Response.fromStream(await _http.send(request));
    } catch (error) {
      throw ApiException(0, '无法连接 ${this.baseUrl}：$error');
    }
    _storeCookies(response);
    if (response.statusCode >= 400) _fail(response);
    if (!cookies.containsKey('tmux_web_session')) {
      throw ApiException(response.statusCode, '登录失败：服务器没有返回会话 cookie');
    }
  }

  @override
  Future<List<Host>> hosts() async {
    final response = await _http.get(_uri('/api/hosts'), headers: _headers());
    final data = _decode(response);
    final raw = (data['hosts'] as List?) ?? const [];
    return raw.whereType<Map>().map((item) => Host.fromJson(Map<String, dynamic>.from(item))).toList();
  }

  @override
  Future<Transcript> transcript(String paneRef, {int limit = 200, bool refresh = false}) async {
    final response = await _http.get(
      _uri('/api/panes/${encodePaneRef(paneRef)}/transcript',
          {'limit': '$limit', if (refresh) 'refresh': '1'}),
      headers: _headers(),
    );
    return Transcript.fromJson(_decode(response));
  }

  /// Headers for the WebSocket handshake (cookie + bearer both work).
  Map<String, String> get wsHeaders => _headers();

  /// ws:// or wss:// URL for a server path.
  Uri wsUri(String path, [Map<String, String>? query]) {
    final base = Uri.parse(baseUrl.isEmpty ? 'http://127.0.0.1' : baseUrl);
    final uri = base.replace(
      scheme: base.scheme == 'https' ? 'wss' : 'ws',
      path: path,
      queryParameters: query,
    );
    return uri;
  }

  @override
  Future<Map<String, String>> paneStates() async {
    final response = await _http.get(_uri('/api/panes/status'), headers: _headers());
    final data = _decode(response);
    final states = <String, String>{};
    for (final item in (data['panes'] as List?) ?? const []) {
      if (item is Map && item['id'] != null) states['${item['id']}'] = '${item['status'] ?? ''}';
    }
    return states;
  }

  @override
  Future<List<RemoteHost>> remoteHosts() async {
    final response = await _http.get(_uri('/api/remote-hosts'), headers: _headers());
    final data = _decode(response);
    return ((data['hosts'] as List?) ?? const [])
        .whereType<Map>()
        .map((item) => RemoteHost.fromJson(Map<String, dynamic>.from(item)))
        .toList();
  }

  @override
  Future<RemoteHost> createRemoteHost(Map<String, dynamic> payload) async {
    final response = await _http.post(
      _uri('/api/remote-hosts'),
      headers: _headers(json: true),
      body: jsonEncode(payload),
    );
    return RemoteHost.fromJson(_decode(response));
  }

  @override
  Future<RemoteHost> updateRemoteHost(String id, Map<String, dynamic> payload) async {
    final response = await _http.put(
      _uri('/api/remote-hosts/${Uri.encodeComponent(id)}'),
      headers: _headers(json: true),
      body: jsonEncode(payload),
    );
    return RemoteHost.fromJson(_decode(response));
  }

  @override
  Future<void> deleteRemoteHost(String id) async {
    final response = await _http.delete(_uri('/api/remote-hosts/${Uri.encodeComponent(id)}'), headers: _headers());
    _decode(response);
  }

  @override
  Future<Map<String, dynamic>> scanHostKey(String hostname, int port) async {
    final response = await _http.post(
      _uri('/api/remote-hosts/scan-key'),
      headers: _headers(json: true),
      body: jsonEncode({'hostname': hostname, 'port': port}),
    );
    return _decode(response);
  }

  @override
  Future<FileListing> listFiles(String paneRef, {String path = '', bool hidden = false}) async {
    final response = await _http.get(
      _uri('/api/panes/${encodePaneRef(paneRef)}/files', {
        if (path.isNotEmpty) 'path': path,
        if (hidden) 'hidden': '1',
      }),
      headers: _headers(),
    );
    return FileListing.fromJson(_decode(response));
  }

  @override
  Future<List<int>> readFile(String paneRef, String path) async {
    final response = await _http.get(
      _uri('/api/panes/${encodePaneRef(paneRef)}/files/raw', {'path': path}),
      headers: _headers(),
    );
    if (response.statusCode >= 400) _fail(response);
    return response.bodyBytes;
  }

  @override
  Future<void> uploadFile(String paneRef, String dir, String name, List<int> bytes) async {
    final response = await _http.put(
      _uri('/api/panes/${encodePaneRef(paneRef)}/files/upload', {'dir': dir, 'name': name}),
      headers: {..._headers(), 'Content-Type': 'application/octet-stream'},
      body: bytes,
    );
    _decode(response);
  }

  @override
  Future<List<int>> zipFiles(String paneRef, List<String> paths, String base) async {
    final response = await _http.post(
      _uri('/api/panes/${encodePaneRef(paneRef)}/files/zip'),
      headers: _headers(json: true),
      body: jsonEncode({'paths': paths, 'base': base}),
    );
    if (response.statusCode >= 400) _fail(response);
    return response.bodyBytes;
  }

  @override
  Future<String> interrupt(String paneRef) async {
    final response = await _http.post(
      _uri('/api/panes/${encodePaneRef(paneRef)}/interrupt'),
      headers: _headers(),
    );
    final data = _decode(response);
    return (data['key'] as String?) ?? 'Escape';
  }

  @override
  Future<void> sendInput(String paneRef, String text, {bool submit = true}) async {
    final response = await _http.post(
      _uri('/api/panes/${encodePaneRef(paneRef)}/input'),
      headers: _headers(json: true),
      body: jsonEncode({'text': text, 'submit': submit}),
    );
    _decode(response);
  }

  @override
  Future<ShareMeta> shareMeta(String slug) async {
    final response = await _http.get(_uri('/share/${Uri.encodeComponent(slug)}/api/meta'), headers: _headers());
    return ShareMeta.fromJson(_decode(response));
  }

  @override
  Future<void> shareUnlock(String slug, String password) async {
    final request = http.Request('POST', _uri('/share/${Uri.encodeComponent(slug)}/login'))
      ..followRedirects = false
      ..headers['Content-Type'] = 'application/x-www-form-urlencoded'
      ..body = Uri(queryParameters: {'password': password}).query;
    final response = await http.Response.fromStream(await _http.send(request));
    _storeCookies(response);
    if (response.statusCode >= 400) _fail(response);
  }

  @override
  Future<Transcript> shareTranscript(String slug, {int limit = 300}) async {
    final meta = await shareMeta(slug);
    final response = await _http.get(
      _uri('/share/${Uri.encodeComponent(slug)}/api/transcript', {'limit': '$limit'}),
      headers: _headers(),
    );
    return Transcript.fromJson(_decode(response), readOnly: !meta.allowInput);
  }

  @override
  Future<void> shareInput(String slug, String text, {bool submit = true}) async {
    final response = await _http.post(
      _uri('/share/${Uri.encodeComponent(slug)}/api/input'),
      headers: _headers(json: true),
      body: jsonEncode({'text': text, 'submit': submit}),
    );
    _decode(response);
  }

  void close() => _http.close();
}
