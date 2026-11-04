import 'dart:io';

import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

/// Native (android/ios/macos) web content view.
///
/// Windows is deliberately NOT a WebView here: webview_flutter 4.14.1
/// ships only android + wkwebview backends, so on Windows this renders a
/// plain fallback note instead of a platform view that could never be
/// created. Callers that need a real page (plugin pages, mermaid) check
/// the platform themselves and show their own unsupported state; this
/// fallback is defense in depth for any caller that forgets.
///
/// Cookie injection happens BEFORE the first load: the session cookie the
/// Dart API client holds is copied into the WebView's cookie store for
/// the target host, so require_auth routes (/assets/..., /plugin-assets/...)
/// see the same session the API calls use.
class WebContentView extends StatefulWidget {
  const WebContentView({
    super.key,
    this.url,
    this.html,
    this.htmlBaseUrl,
    this.cookies = const {},
    this.onLoaded,
    this.onError,
    this.onMermaidError,
  }) : assert(url != null || html != null, 'WebContentView needs url or html');

  /// Remote page to load (plugin pages). Mutually exclusive with [html]
  /// in practice; [html] wins if both are given.
  final Uri? url;

  /// Inline document to load (mermaid rendering). Its script tags use
  /// absolute server URLs, so it does not depend on [htmlBaseUrl] for
  /// asset resolution, but the base is still passed to loadHtmlString.
  final String? html;

  /// Base URL passed to loadHtmlString for [html] documents.
  final String? htmlBaseUrl;

  /// Cookies to inject for the target host before loading, e.g. the
  /// `tmux_web_session` cookie parsed from the API client's Cookie header.
  final Map<String, String> cookies;

  final VoidCallback? onLoaded;

  /// Main-frame load failures. Subresource failures (a 404 favicon, a
  /// failed image) are NOT forwarded: they must not turn a usable page
  /// into an error screen.
  final ValueChanged<String>? onError;

  /// Render failures reported by inline documents via the `MermaidError`
  /// JavaScript channel (mermaid pages only).
  final ValueChanged<String>? onMermaidError;

  @override
  State<WebContentView> createState() => _WebContentViewState();
}

class _WebContentViewState extends State<WebContentView> {
  late final WebViewController _controller;
  bool _isWindows = false;

  @override
  void initState() {
    super.initState();
    _isWindows = Platform.isWindows;
    if (_isWindows) return;
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(Colors.transparent)
      ..setNavigationDelegate(
        NavigationDelegate(
          onPageFinished: (_) => widget.onLoaded?.call(),
          onWebResourceError: (error) {
            // isForMainFrame == false means a subresource failed; only a
            // main-frame failure is a page failure.
            if (error.isForMainFrame == false) return;
            widget.onError?.call('加载失败：${error.description}');
          },
          onHttpError: (error) {
            // Only the page's own URL counts: a plugin page that loads a
            // 404 subresource is still a usable page. Inline (mermaid)
            // documents have no page URL, and their script/render failures
            // surface through the MermaidError channel and the in-page
            // error container instead.
            final pageUrl = widget.url;
            if (pageUrl == null) return;
            if (error.request?.uri != pageUrl) return;
            widget.onError?.call('加载失败：HTTP ${error.response?.statusCode ?? ''}');
          },
        ),
      )
      ..addJavaScriptChannel(
        'MermaidError',
        onMessageReceived: (message) => widget.onMermaidError?.call(message.message),
      );
    _load();
  }

  Future<void> _load() async {
    final target = widget.url ?? (widget.htmlBaseUrl == null ? null : Uri.tryParse(widget.htmlBaseUrl!));
    final host = target?.host ?? '';
    if (host.isNotEmpty && widget.cookies.isNotEmpty) {
      final manager = WebViewCookieManager();
      for (final entry in widget.cookies.entries) {
        try {
          await manager.setCookie(WebViewCookie(name: entry.key, value: entry.value, domain: host, path: '/'));
        } catch (_) {
          // One un-settable cookie must not block the page; the server
          // will answer 401/403 and the error path reports that instead.
        }
      }
    }
    if (!mounted) return;
    try {
      if (widget.html != null) {
        await _controller.loadHtmlString(widget.html!, baseUrl: widget.htmlBaseUrl);
      } else if (widget.url != null) {
        await _controller.loadRequest(widget.url!);
      }
    } catch (error) {
      if (mounted) widget.onError?.call('加载失败：$error');
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_isWindows) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(16),
          child: Text(
            '当前平台（Windows）暂不支持内嵌网页渲染',
            style: TextStyle(fontSize: 12),
            textAlign: TextAlign.center,
          ),
        ),
      );
    }
    return WebViewWidget(controller: _controller);
  }
}
