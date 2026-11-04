import 'dart:js_interop';
import 'dart:ui_web' as ui_web;

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;

/// Flutter-web web content view: an iframe via HtmlElementView.
///
/// Same-origin by construction: plugin pages load from the server origin
/// and mermaid documents are injected via srcdoc (which inherits the
/// parent origin), so the browser sends its own cookies to the
/// require_auth routes automatically — no cookie injection is needed or
/// possible here, and [cookies] is ignored.
///
/// Browsers do not reliably surface iframe HTTP errors, so a disabled
/// plugin's 404 page would render inside the iframe instead of reaching
/// [onError] if the iframe were loaded blindly. This widget therefore
/// preflights a [url] page with a same-origin HEAD request BEFORE the
/// iframe element is created: only a non-error status builds the
/// [HtmlElementView], so the iframe never loads a failed page. A 404 (or
/// any other error status, or a failed check) calls [onError] instead,
/// and the widget renders the error text. The native implementation
/// detects the same case via onHttpError and is unchanged.
///
/// Inline [html] documents (mermaid) have no page URL to preflight, so
/// they skip the check and render immediately, as before.
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
    this.preflight,
  }) : assert(url != null || html != null, 'WebContentView needs url or html');

  final Uri? url;
  final String? html;
  final String? htmlBaseUrl;
  final Map<String, String> cookies;
  final VoidCallback? onLoaded;
  final ValueChanged<String>? onError;
  final ValueChanged<String>? onMermaidError;

  /// Test seam / override for the preflight check. Production passes null
  /// and the widget issues a same-origin HEAD request against [url].
  /// Returns the HTTP status code, or null when the check itself fails.
  final PluginAssetPreflight? preflight;

  @override
  State<WebContentView> createState() => _WebContentViewState();
}

/// Preflight check for a plugin asset URL: the HTTP status code, or null
/// when the check itself fails (network error, timeout, ...).
typedef PluginAssetPreflight = Future<int?> Function(Uri url);

/// Default preflight: a same-origin HEAD request. HEAD is safe here — the
/// server is FastAPI/Starlette, which answers HEAD on every GET route
/// with the GET route's status and no body — so a disabled plugin's 404
/// (and a 403 for a missing `assets` permission) surfaces without
/// downloading the page. package:http's browser client sends the
/// browser's own cookies for same-origin requests, so require_auth routes
/// see the same session the iframe would.
Future<int?> _defaultPreflight(Uri url) async {
  try {
    final response = await http.head(url);
    return response.statusCode;
  } catch (_) {
    return null;
  }
}

class _WebContentViewState extends State<WebContentView> {
  static int _counter = 0;
  late final String _viewType;

  /// True once the iframe may be built: immediately for srcdoc documents,
  /// only after a successful preflight for [WebContentView.url] pages.
  bool _ready = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _viewType = 'tmuxweb-web-content-${_counter++}';
    ui_web.platformViewRegistry.registerViewFactory(_viewType, (int viewId) {
      final iframe = web.HTMLIFrameElement()
        ..style.width = '100%'
        ..style.height = '100%'
        ..style.border = '0';
      if (widget.html != null) {
        iframe.srcdoc = widget.html!.toJS;
      } else if (widget.url != null) {
        iframe.src = widget.url.toString();
      }
      iframe.onLoad.listen((_) {
        if (mounted) widget.onLoaded?.call();
      });
      return iframe;
    });
    final url = widget.url;
    if (widget.html != null || url == null) {
      // No page URL to preflight (srcdoc document).
      _ready = true;
    } else {
      _preflight(url);
    }
  }

  Future<void> _preflight(Uri url) async {
    final check = widget.preflight ?? _defaultPreflight;
    final status = await check(url);
    if (!mounted) return;
    if (status != null && status < 400) {
      setState(() => _ready = true);
      return;
    }
    // 404 (disabled plugin / missing page), any other error status, or a
    // check that failed outright: never build the iframe, surface the
    // Flutter error state instead. The message shape matches the native
    // onHttpError text, so PluginPageScreen's mapping (404 → disabled
    // message, 403 → permission message) applies unchanged.
    final message = status == null ? '加载失败：无法连接服务器' : '加载失败：HTTP $status';
    setState(() => _error = message);
    widget.onError?.call(message);
  }

  @override
  Widget build(BuildContext context) {
    final error = _error;
    if (error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(error, style: const TextStyle(fontSize: 13), textAlign: TextAlign.center),
        ),
      );
    }
    // While the preflight is in flight, render nothing: the iframe
    // element — and therefore the page load — only exists after success.
    // Callers that show their own spinner (PluginPageScreen) keep showing
    // it until onLoaded / onError fires.
    if (!_ready) return const SizedBox.shrink();
    return HtmlElementView(viewType: _viewType);
  }
}
