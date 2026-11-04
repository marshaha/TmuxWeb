import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:webview_windows/webview_windows.dart' as win;

import 'web_content_view_io.dart' as io_view;

/// Windows web content view (WebView2 via webview_windows 0.4.0), with
/// the same public API as [io_view.WebContentView] so callers
/// (mermaid rendering, plugin pages) never branch on the platform.
///
/// Seam note: Dart conditional imports have no Windows condition
/// (`dart.library.*` cannot distinguish Windows from the other
/// `dart:io` targets), so the seam in `web_content_view.dart` routes
/// every non-web target here and THIS file runtime-dispatches:
/// Windows gets the WebView2 implementation below, every other native
/// target delegates back to the io implementation unchanged. This mirrors
/// the runtime `_isWindows` gate the io implementation used to carry.
///
/// Why WebView2 needs different auth handling than webview_flutter:
///
/// * webview_windows 0.4.0 has NO cookie API — only `clearCookies`
///   (DevTools `Network.clearBrowserCookies`); there is no set-cookie
///   call to copy the session into before loading. So URL loads do two
///   steps: an unauthenticated load first (the server answers require_auth
///   routes with HTTP 401 from its own origin, never a redirect), then
///   the session cookies are written with `document.cookie` via
///   `executeScript` into that same-origin document, then the target is
///   loaded again — the second request carries the cookies from the
///   WebView2 profile jar. One un-settable cookie must not block the
///   page, same as the io implementation's per-cookie try/catch.
/// * Inline (mermaid) documents cannot use `loadStringContent`:
///   WebView2 `NavigateToString` yields an opaque-origin document, and a
///   `<script src>` fetch from an opaque origin is cross-origin without
///   credentials — the vendored `/assets/mermaid/mermaid.min.js` is a
///   require_auth route, so it would 401. Instead the document is
///   written with `document.write` into a same-origin page (navigated to
///   at [htmlBaseUrl] first, cookies seeded the same way), so the
///   written document keeps the navigation origin and its script fetch
///   is same-origin and authenticated. The document itself is untouched
///   (built by `mermaid_view.dart`: server-vendored script, theme
///   'neutral', securityLevel 'strict', flowchart htmlLabels false);
///   only the `MermaidError` shim below is prepended (see below).
/// * webview_windows has no JavaScript channels. The page's
///   `window.MermaidError.postMessage` (which the mermaid document calls
///   on render failure) is shimmed to
///   `window.chrome.webview.postMessage`, delivered to Dart via the
///   controller's `webMessage` stream and forwarded to [onMermaidError].
///   The shim is inserted into the document head, before the page's own
///   scripts run; if the document has neither `<head>` nor `<body>` the
///   shim is skipped and the in-page error line still shows.
/// * HTTP errors are NOT navigation errors in WebView2: the native
///   `NavigationCompleted` handler forwards to `onLoadError` only when
///   `IsSuccess` is false (network-level failures), so a disabled
///   plugin's 404 or a 403 completes "successfully". URL loads therefore
///   probe the status afterwards with a same-origin `fetch` of the entry
///   URL (static asset, safe to re-GET) and report
///   `加载失败：HTTP <status>` in the io implementation's message format,
///   so `pluginLoadErrorMessage` maps 404/403 unchanged. A failed probe
///   fails open (the page itself rendered). Subresource failures are
///   never surfaced by this plugin's API, which matches the io
///   implementation's main-frame-only rule in effect.
/// * NavigationCompleted can fire for subframes too, so a completion is
///   only acted on when the top document's host (via `executeScript`
///   `window.location.hostname`) matches the target host; anything else
///   keeps waiting. A location probe that throws fails open.
/// * Transparent backgrounds are not supported by the plugin (non-zero
///   alpha is forced opaque), so the surface is opaque white instead of
///   the io implementation's transparent background.
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

  /// Inline document to load (mermaid rendering).
  final String? html;

  /// Base URL: the same-origin page navigated to before [html] is
  /// written into it, and the origin the mermaid script is fetched from.
  final String? htmlBaseUrl;

  /// Cookies to inject for the target host, e.g. the `tmux_web_session`
  /// cookie parsed from the API client's Cookie header.
  final Map<String, String> cookies;

  final VoidCallback? onLoaded;

  /// Main-frame load failures, and HTTP >= 400 probed on URL loads.
  final ValueChanged<String>? onError;

  /// Render failures reported by inline documents via the `MermaidError`
  /// shim (mermaid pages only).
  final ValueChanged<String>? onMermaidError;

  @override
  State<WebContentView> createState() => _WebContentViewState();
}

class _WebContentViewState extends State<WebContentView> {
  win.WebviewController? _controller;
  StreamSubscription<win.LoadingState>? _loadingSub;
  StreamSubscription<win.WebErrorStatus>? _errorSub;
  StreamSubscription<dynamic>? _messageSub;

  /// True while a navigation we are waiting on is in flight. Cleared
  /// before the cookie-injection step so a late subframe completion
  /// from the previous document cannot be mistaken for the reload's.
  bool _expectingCompletion = false;

  /// A terminal callback ([onLoaded] or [onError]) has been sent; later
  /// completions and errors are ignored.
  bool _reported = false;

  /// URL loads only: cookies injected and the reload kicked off.
  bool _injected = false;

  /// HTML loads only: the same-origin base the document is written into,
  /// or null when falling back to a plain string document.
  Uri? _htmlBase;

  @override
  void initState() {
    super.initState();
    if (!Platform.isWindows) return;
    final controller = win.WebviewController();
    _controller = controller;
    _loadingSub = controller.loadingState.listen(_onLoadingState);
    _errorSub = controller.onLoadError.listen(_onLoadError);
    _messageSub = controller.webMessage.listen(_onWebMessage);
    _start();
  }

  Future<void> _start() async {
    final controller = _controller;
    if (controller == null) return;
    try {
      await controller.initialize();
      if (!mounted) return;
      // Rebuild so the Webview widget shows the texture now that the
      // controller is initialized (it renders an empty box until then).
      setState(() {});
      try {
        await controller.setBackgroundColor(Colors.white);
      } catch (_) {
        // Background is cosmetic; a failure must not block the page.
      }
      try {
        // Start from a clean jar: a stale session cookie from an earlier
        // run must not shadow the cookies the caller passes in.
        await controller.clearCookies();
      } catch (_) {
        // Same as above: injection below still runs.
      }
      if (!mounted) return;
      if (widget.html != null) {
        await _loadHtml(controller);
      } else if (widget.url != null) {
        _expectingCompletion = true;
        await controller.loadUrl(widget.url!.toString());
      }
    } catch (error) {
      _fail('加载失败：$error');
    }
  }

  Future<void> _loadHtml(win.WebviewController controller) async {
    final base = widget.htmlBaseUrl == null ? null : Uri.tryParse(widget.htmlBaseUrl!);
    if (base == null || (base.scheme != 'http' && base.scheme != 'https')) {
      // No same-origin page to seed from: fall back to an opaque-origin
      // string document. Its absolute-URL fetches go out unauthenticated,
      // so a require_auth script fetch fails and surfaces through the
      // in-page error line and onMermaidError instead of rendering.
      _htmlBase = null;
      _expectingCompletion = true;
      await controller.loadStringContent(_withMermaidShim(widget.html!));
      return;
    }
    _htmlBase = base;
    _expectingCompletion = true;
    await controller.loadUrl(base.toString());
  }

  Future<void> _onLoadingState(win.LoadingState state) async {
    if (state != win.LoadingState.navigationCompleted) return;
    if (!_expectingCompletion || _reported) return;
    final controller = _controller;
    if (controller == null) return;
    final topHost = await _topHostname(controller);
    if (widget.html != null) {
      final base = _htmlBase;
      if (base == null) {
        // Fallback string document finished loading.
        _expectingCompletion = false;
        _finishLoaded();
        return;
      }
      if (topHost != null && topHost != base.host) return;
      _expectingCompletion = false;
      await _writeHtml(controller);
      return;
    }
    final target = widget.url;
    if (target == null) return;
    if (topHost != null && topHost != target.host) return;
    if (!_injected && widget.cookies.isNotEmpty) {
      _injected = true;
      _expectingCompletion = false;
      try {
        await _injectCookies(controller);
      } catch (_) {
        // Injection failure surfaces as the server's 401 page on the
        // reload; the error path reports that instead of blocking here.
      }
      if (!mounted || _reported) return;
      _expectingCompletion = true;
      try {
        await controller.loadUrl(target.toString());
      } catch (error) {
        _fail('加载失败：$error');
      }
      return;
    }
    _expectingCompletion = false;
    await _probeAndFinish(controller, target);
  }

  void _onLoadError(win.WebErrorStatus status) {
    if (!_expectingCompletion || _reported) return;
    _expectingCompletion = false;
    _fail('加载失败：$status');
  }

  void _onWebMessage(dynamic message) {
    if (message is Map && message['channel'] == 'MermaidError' && message['message'] is String) {
      widget.onMermaidError?.call(message['message'] as String);
    }
  }

  /// Writes the inline document into the already-loaded same-origin
  /// page, so its absolute-URL script fetch is same-origin and carries
  /// the seeded session cookies.
  Future<void> _writeHtml(win.WebviewController controller) async {
    if (widget.cookies.isNotEmpty) {
      try {
        await _injectCookies(controller);
      } catch (_) {
        // As above: the script fetch then 401s and the page reports it.
      }
    }
    if (!mounted || _reported) return;
    try {
      await controller.executeScript(
        'document.open();document.write(${jsonEncode(_withMermaidShim(widget.html!))});document.close();',
      );
    } catch (error) {
      _fail('加载失败：$error');
      return;
    }
    _finishLoaded();
  }

  /// Probes the entry URL's HTTP status (WebView2 reports HTTP errors
  /// as successful navigations) and finishes the load.
  Future<void> _probeAndFinish(win.WebviewController controller, Uri target) async {
    int? status;
    try {
      final result = await controller.executeScript(
        'fetch(${jsonEncode(target.toString())}).then(function(r){return r.status;}).catch(function(){return -1;})',
      );
      if (result is num) status = result.toInt();
    } catch (_) {
      status = null;
    }
    if (!mounted || _reported) return;
    if (status != null && status >= 400) {
      _fail('加载失败：HTTP $status');
    } else {
      _finishLoaded();
    }
  }

  /// Writes each cookie into the current (same-origin) document's jar.
  /// Values are token-like (no `;` or whitespace), matching what the io
  /// implementation passes to WebViewCookie raw.
  Future<void> _injectCookies(win.WebviewController controller) async {
    if (widget.cookies.isEmpty) return;
    final script = widget.cookies.entries
        .map((entry) => 'document.cookie=${jsonEncode('${entry.key}=${entry.value}; path=/')};')
        .join();
    await controller.executeScript(script);
  }

  /// Top document's hostname, or null when it cannot be determined
  /// (callers fail open on null).
  Future<String?> _topHostname(win.WebviewController controller) async {
    try {
      final result = await controller.executeScript('window.location.hostname');
      if (result is String && result.isNotEmpty) return result;
    } catch (_) {
      // Fail open: act on the completion rather than hanging.
    }
    return null;
  }

  void _finishLoaded() {
    if (_reported) return;
    _reported = true;
    if (mounted) widget.onLoaded?.call();
  }

  void _fail(String message) {
    if (_reported) return;
    _reported = true;
    if (mounted) widget.onError?.call(message);
  }

  @override
  void dispose() {
    _loadingSub?.cancel();
    _errorSub?.cancel();
    _messageSub?.cancel();
    final controller = _controller;
    if (controller != null) {
      // Fire and forget: dispose awaits the creation future, which
      // rethrows if initialization failed; that must not escape dispose.
      controller.dispose().then((_) {}, onError: (_) {});
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (!Platform.isWindows) {
      // Not Windows: behavior is exactly the io implementation.
      return io_view.WebContentView(
        url: widget.url,
        html: widget.html,
        htmlBaseUrl: widget.htmlBaseUrl,
        cookies: widget.cookies,
        onLoaded: widget.onLoaded,
        onError: widget.onError,
        onMermaidError: widget.onMermaidError,
      );
    }
    final controller = _controller;
    if (controller == null) return const SizedBox.shrink();
    return win.Webview(controller);
  }
}

/// Defines `window.MermaidError.postMessage` in terms of WebView2's
/// `window.chrome.webview.postMessage`, so the mermaid document's render
/// failures reach Dart's `webMessage` stream. Inserted before the page's
/// own scripts; the page reads `window.MermaidError` at call time, so
/// any position ahead of the render call works.
String _withMermaidShim(String html) {
  const shim =
      "<script>window.MermaidError={postMessage:function(m){try{window.chrome.webview.postMessage({channel:'MermaidError',message:String(m)});}catch(e){}}};</script>";
  if (html.contains('<head>')) return html.replaceFirst('<head>', '<head>$shim');
  if (html.contains('<body>')) return html.replaceFirst('<body>', '<body>$shim');
  return html;
}
