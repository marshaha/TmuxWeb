import 'dart:convert';

import 'package:flutter/material.dart';

import '../theme.dart';
import 'web_content_view.dart';

/// Mermaid rendering identical to the web console (web/index.html
/// loadMermaid / enhanceBlocks):
///
/// * the SAME vendored script the server serves — `/assets/mermaid/
///   mermaid.min.js` (require_auth, Cache-Control public max-age=604800)
///   — loaded from the server URL, never a bundled copy, so version
///   identity holds by construction;
/// * initialization EXACTLY like web: startOnLoad:false, theme:'neutral',
///   securityLevel:'strict', flowchart.htmlLabels:false,
///   fontFamily:'ui-sans-serif,system-ui';
/// * render via mermaid.render(...) into an SVG target; on failure the
///   page shows an inline `mermaid 渲染失败：…` line (web :1141) and reports
///   the failure to Flutter, which falls back to the source view.
///
/// The document is built by the pure function [buildMermaidHtml] so tests
/// can cover init params, security settings and escaping without ever
/// creating a platform view. [MermaidView] itself creates a platform view
/// only when [createPlatformView] is true; tests pass false and get a
/// placeholder box instead. Windows has no webview_flutter backend, so
/// callers (BlockView) never render this widget on Windows at all.
String mermaidScriptUrl(String assetBaseUrl) {
  var base = assetBaseUrl.trim();
  while (base.endsWith('/')) {
    base = base.substring(0, base.length - 1);
  }
  return '$base/assets/mermaid/mermaid.min.js';
}

String _htmlEscape(String value) =>
    value.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');

/// Pure HTML document for one diagram. [source] is embedded as a JSON
/// string literal, with `</` neutralized to `<\/` so a diagram containing
/// `</script>` cannot break out of the inline script block.
String buildMermaidHtml(String source, String assetBaseUrl) {
  final encoded = jsonEncode(source).replaceAll('</', '<\\/');
  final scriptUrl = _htmlEscape(mermaidScriptUrl(assetBaseUrl));
  return '''
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body { margin: 0; padding: 8px; font-family: ui-sans-serif, system-ui, sans-serif; background: transparent; }
#mermaid-target { overflow-x: auto; }
#mermaid-target svg { max-width: 100%; height: auto; }
#mermaid-error { color: #b3261e; font-size: 12px; line-height: 1.5; white-space: pre-wrap; }
</style>
</head>
<body>
<div id="mermaid-target"><span>渲染中…</span></div>
<div id="mermaid-error" hidden></div>
<script src="$scriptUrl"></script>
<script>
function showMermaidError(error) {
  var message = (error && error.message) ? error.message : String(error);
  var el = document.getElementById('mermaid-error');
  el.hidden = false;
  el.textContent = 'mermaid 渲染失败：' + message;
  if (window.MermaidError) { try { window.MermaidError.postMessage(message); } catch (e) {} }
}
try {
  if (typeof mermaid === 'undefined') {
    showMermaidError(new Error('无法加载 mermaid'));
  } else {
    mermaid.initialize({startOnLoad:false, theme:'neutral', securityLevel:'strict', flowchart:{htmlLabels:false}, fontFamily:'ui-sans-serif,system-ui'});
    mermaid.render('mermaid-diagram', $encoded).then(function(result) {
      document.getElementById('mermaid-target').innerHTML = (result && result.svg) ? result.svg : String(result || '');
    }).catch(function(error) { showMermaidError(error); });
  }
} catch (error) { showMermaidError(error); }
</script>
</body>
</html>
''';
}

/// Renders one mermaid diagram. The toolbar (copy + 源码 toggle) lives in
/// the caller (BlockView), mirroring web's per-block bar; this widget only
/// owns the rendered view and the render-failure fallback to source.
class MermaidView extends StatefulWidget {
  const MermaidView({
    super.key,
    required this.source,
    required this.assetBaseUrl,
    this.cookieHeader = '',
    this.createPlatformView = true,
  });

  final String source;

  /// Server base URL, e.g. `http://host:8899`. The mermaid script is
  /// fetched from `<base>/assets/mermaid/mermaid.min.js`.
  final String assetBaseUrl;

  /// `Cookie` header value from the API client (native only; the web
  /// target's iframe uses the browser's own cookies).
  final String cookieHeader;

  /// False in tests: renders a placeholder instead of a platform view,
  /// which cannot run under `flutter test`.
  final bool createPlatformView;

  @override
  State<MermaidView> createState() => _MermaidViewState();
}

class _MermaidViewState extends State<MermaidView> {
  String? _error;

  @override
  Widget build(BuildContext context) {
    if (_error != null) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 10, 12, 0),
            child: Text('mermaid 渲染失败：$_error', style: TextStyle(fontSize: 12, color: AppTheme.danger, height: 1.4)),
          ),
          _MermaidSourceView(source: widget.source),
        ],
      );
    }
    if (!widget.createPlatformView) {
      return const SizedBox(
        key: Key('mermaid-platform-placeholder'),
        height: 320,
        child: Center(child: Text('Mermaid 渲染视图', style: TextStyle(fontSize: 12))),
      );
    }
    return SizedBox(
      height: 320,
      child: WebContentView(
        html: buildMermaidHtml(widget.source, widget.assetBaseUrl),
        htmlBaseUrl: widget.assetBaseUrl,
        cookies: parseCookieHeader(widget.cookieHeader),
        onMermaidError: (message) {
          if (mounted) setState(() => _error = message);
        },
      ),
    );
  }
}

class _MermaidSourceView extends StatelessWidget {
  const _MermaidSourceView({required this.source});

  final String source;

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: ConstrainedBox(
        constraints: const BoxConstraints(minWidth: 260),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: SelectableText(source.trim(), style: AppTheme.mono),
        ),
      ),
    );
  }
}
