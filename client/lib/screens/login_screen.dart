import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/api_client.dart';
import '../theme.dart';
import 'hosts_screen.dart';

/// Login: server URL plus password (or an API token), then straight into the
/// host/pane browser. The URL is remembered, the password never is.
class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key, this.apiFactory, this.initialBaseUrl = ''});

  /// Injectable for tests; receives the normalized base URL.
  final Api Function(String baseUrl)? apiFactory;
  final String initialBaseUrl;

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  static const _prefsKey = 'tmuxweb.baseUrl';

  final _baseUrl = TextEditingController();
  final _password = TextEditingController();
  final _token = TextEditingController();
  SharedPreferences? _prefs;
  bool _busy = false;
  bool _obscure = true;
  bool _advanced = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _baseUrl.text = widget.initialBaseUrl;
    _restoreUrl();
  }

  @override
  void dispose() {
    _baseUrl.dispose();
    _password.dispose();
    _token.dispose();
    super.dispose();
  }

  Future<void> _restoreUrl() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      if (!mounted) return;
      setState(() {
        _prefs = prefs;
        if (_baseUrl.text.isEmpty) _baseUrl.text = prefs.getString(_prefsKey) ?? '';
      });
    } catch (_) {
      // preferences are a convenience only
    }
  }

  Future<void> _submit() async {
    if (_busy) return;
    if (_password.text.isEmpty && _token.text.isEmpty) {
      setState(() => _error = '请填写密码或 API Token');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final baseUrl = normalizeBaseUrl(_baseUrl.text);
      final api = (widget.apiFactory ?? (url) => ApiClient(baseUrl: url))(baseUrl);
      await api.login(baseUrl: baseUrl, password: _password.text, token: _token.text);
      await _prefs?.setString(_prefsKey, baseUrl);
      if (!mounted) return;
      await Navigator.of(context).pushReplacement(
        MaterialPageRoute(builder: (_) => HostsScreen(api: api, baseUrl: baseUrl)),
      );
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = '$error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 430),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const _Logo(),
                  const SizedBox(height: 22),
                  const Text('连接你的 TmuxWeb', style: TextStyle(fontSize: 22, fontWeight: FontWeight.w700)),
                  const SizedBox(height: 6),
                  const Text(
                    '本机与远程主机上的 AI 会话，原生界面直接读写。',
                    style: TextStyle(fontSize: 13, color: AppTheme.muted, height: 1.5),
                  ),
                  const SizedBox(height: 26),
                  const _Label('服务器地址'),
                  TextField(
                    key: const Key('login-base-url'),
                    controller: _baseUrl,
                    keyboardType: TextInputType.url,
                    autocorrect: false,
                    enableSuggestions: false,
                    textInputAction: TextInputAction.next,
                    decoration: const InputDecoration(hintText: 'http://10.168.236.31:8899'),
                  ),
                  const SizedBox(height: 14),
                  const _Label('密码'),
                  TextField(
                    key: const Key('login-password'),
                    controller: _password,
                    obscureText: _obscure,
                    autocorrect: false,
                    enableSuggestions: false,
                    textInputAction: TextInputAction.go,
                    onSubmitted: (_) => _submit(),
                    decoration: InputDecoration(
                      hintText: 'TMUX_WEB_PASSWORD',
                      suffixIcon: IconButton(
                        tooltip: _obscure ? '显示密码' : '隐藏密码',
                        icon: Icon(_obscure ? Icons.visibility_off_outlined : Icons.visibility_outlined, size: 20),
                        onPressed: () => setState(() => _obscure = !_obscure),
                      ),
                    ),
                  ),
                  const SizedBox(height: 8),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: TextButton(
                      onPressed: () => setState(() => _advanced = !_advanced),
                      style: TextButton.styleFrom(padding: EdgeInsets.zero, minimumSize: const Size(0, 32)),
                      child: Text(_advanced ? '收起 API Token' : '改用 API Token', style: const TextStyle(fontSize: 12.5)),
                    ),
                  ),
                  if (_advanced) ...[
                    const _Label('API Token'),
                    TextField(
                      key: const Key('login-token'),
                      controller: _token,
                      autocorrect: false,
                      enableSuggestions: false,
                      decoration: const InputDecoration(hintText: 'TMUX_WEB_API_TOKEN'),
                    ),
                  ],
                  if (_error != null) ...[
                    const SizedBox(height: 12),
                    Text(_error!, key: const Key('login-error'), style: const TextStyle(color: Color(0xFFB34B43), fontSize: 12.5)),
                  ],
                  const SizedBox(height: 20),
                  FilledButton(
                    key: const Key('login-submit'),
                    onPressed: _busy ? null : _submit,
                    child: _busy
                        ? const SizedBox(height: 18, width: 18, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                        : const Text('连接'),
                  ),
                  const SizedBox(height: 14),
                  const Text(
                    '地址会记在这台设备上，密码只用来换取登录 cookie，不会保存。',
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 11.5, color: AppTheme.faint, height: 1.5),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _Logo extends StatelessWidget {
  const _Logo();

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Container(
          height: 40,
          width: 40,
          decoration: BoxDecoration(
            gradient: const LinearGradient(colors: [Color(0xFF35332C), Color(0xFF181713)], begin: Alignment.topLeft, end: Alignment.bottomRight),
            borderRadius: BorderRadius.circular(10),
          ),
          alignment: Alignment.center,
          child: const Text('>', style: TextStyle(color: Color(0xFFF7F4EA), fontSize: 22, fontFamily: 'monospace', height: 1.1)),
        ),
        const SizedBox(width: 10),
        const Text('TmuxWeb', style: TextStyle(fontSize: 17, fontWeight: FontWeight.w700, letterSpacing: 0.2)),
      ],
    );
  }
}

class _Label extends StatelessWidget {
  const _Label(this.text);

  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 6),
        child: Text(text, style: const TextStyle(fontSize: 11.5, color: AppTheme.muted)),
      );
}
