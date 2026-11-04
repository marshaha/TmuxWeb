import 'dart:js_interop';
import 'dart:ui_web' as ui_web;

import 'package:flutter/material.dart';
import 'package:web/web.dart' as web;

/// Flutter-web web content view: an iframe via HtmlElementView.
///
/// Same-origin by construction: plugin pages load from the server origin
/// and mermaid documents are injected via srcdoc (which inherits the
/// parent origin), so the browser sends its own cookies to the
/// require_auth routes automatically — no cookie injection is needed or
/// possible here, and [cookies] is ignored.
///
/// Known limit: browsers do not reliably surface iframe HTTP errors, so
/// a disabled plugin's 404 page may render inside the iframe instead of
/// reaching [onError]. The native implementation detects that case via
/// onHttpError; the web target cannot.
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

  final Uri? url;
  final String? html;
  final String? htmlBaseUrl;
  final Map<String, String> cookies;
  final VoidCallback? onLoaded;
  final ValueChanged<String>? onError;
  final ValueChanged<String>? onMermaidError;

  @override
  State<WebContentView> createState() => _WebContentViewState();
}

class _WebContentViewState extends State<WebContentView> {
  static int _counter = 0;
  late final String _viewType;

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
  }

  @override
  Widget build(BuildContext context) => HtmlElementView(viewType: _viewType);
}
