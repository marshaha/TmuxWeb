import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
// markdown is flutter_markdown's public API type (InlineSyntax /
// Element appear in MarkdownBody's own signatures); it is a transitive
// dependency, and pubspec.yaml is outside this batch's scope.
// ignore: depend_on_referenced_packages
import 'package:markdown/markdown.dart' as md;

import '../theme.dart';

/// Path links in chat text, mirroring web/assets/path-links.js.
///
/// Detection rules (ported from path-links.js):
/// * a path starts at the start of the text or after a boundary char
/// (whitespace, brackets/quotes, or Chinese punctuation),
/// * absolute paths (`/a/b`, `~/x`, `./x`, `../x`) or relative paths with
/// an extension (`results/out.md`, `src/main.py`),
/// * at least 2 segments, unless the path starts with `~/`, `./` or `../`,
/// * paths under the web prefix (`/api/`, `/assets/`, `/static/`,
/// `/vendor/`, `/mcp/`, `/share-assets/`) are server routes, not files,
/// * URLs are skipped (a scheme directly before the slashes vetoes the
/// match, which also covers web's 8-char lookback case).
///
/// Nothing is resolved at build time: [PathLinkActions.resolve] (one
/// `fileStat` call — the server itself probes pane cwd → parent → home
/// and returns the absolute path plus the matching `offset`) runs only
/// when a link is tapped, and the result is cached per widget, so a
/// message is never stat'ed at load and a link is stat'ed at most once
/// per tile lifetime. Tapping a link never downloads directly: it opens
/// a bottom sheet with a Preview / Download choice, and text files are
/// previewed, never downloaded on tap (web rule from commit e788d4a).
class PathLinkSegment {
  const PathLinkSegment(this.text, {this.path});

  /// Plain text, or the link label when [path] is non-null.
  final String text;
  final String? path;

  bool get isLink => path != null;
}

/// Web's TEXT_EXT list from path-links.js: previewable text files.
final RegExp _textExt = RegExp(
    r'\.(md|markdown|mdx|txt|log|json|ya?ml|csv|tsv|ini|conf|cfg|env|toml|xml|html?|css|js|ts|py|sh|sql)$',
    caseSensitive: false);

bool isTextPath(String path) => _textExt.hasMatch(path);

/// Web's WEB_PREFIX: interface / static-asset routes are not local paths.
final RegExp _webPrefix =
    RegExp(r'^/(api|assets|static|vendor|mcp|share-assets)/');

/// Web's BOUNDARY set from path-links.js, as a plain set so no regex
/// character class has to spell out quotes and brackets.
/// Web's BOUNDARY set from path-links.js, as code points so the
/// source stays pure ASCII.
final Set<String> _boundary = {
  for (final c in [
    0x0020,
    0x0009,
    0x000A,
    0x000D,
    0x0028,
    0xFF08,
    0x005B,
    0x3010,
    0x003C,
    0x0022,
    0x0027,
    0x0060,
    0xFF1A,
    0x003A,
    0xFF0C,
    0x3001,
    0xFF1B,
    0x003B,
    0xFF01,
    0xFF1F,
    0x0021,
    0x003F,
    0xFF09,
    0x3011,
    0x300D,
    0x300F,
    0x300B,
    0x003E,
  ])
    String.fromCharCode(c),
};

/// Hex escapes keep double quote, single quote and backtick out of the
/// pattern source: \x22 = ", \x27 = ', \x60 = `.
final RegExp _absolutePattern = RegExp(
    r'((?:~|\.{1,2})?\/[^\s\x22\x27\x60<>|\uFF0C\u3002\uFF1B\uFF1A\u3001\uFF09)\u3011]+)');
final RegExp _relativePattern =
    RegExp(r'((?:\.{1,2}\/)?(?:[\w@%+.-]+\/)+[\w@%+.-]+\.[A-Za-z0-9]{1,8})');
final RegExp _trailingStrip =
    RegExp(r'[.,;:!?)}\s\u3011\u3002\uFF0C\uFF1B\uFF1A\uFF01\uFF1F]+$');
final RegExp _hasPathChar = RegExp(r'[A-Za-z0-9_@%+-]');
final RegExp _pathChar = RegExp(r'[\w@%+.-]');

String _cleanPath(String raw) {
  final cleaned = raw.replaceAll(_trailingStrip, '');
  if (cleaned.length < 2) return '';
  if (!_hasPathChar.hasMatch(cleaned)) return '';
  return cleaned;
}

bool _plausible(String path) {
  if (_webPrefix.hasMatch(path)) return false;
  if (path.startsWith('~/') ||
      path.startsWith('./') ||
      path.startsWith('../')) {
    return true;
  }
  return path.split('/').where((part) => part.isNotEmpty).length >= 2;
}

bool _isUrl(String text, int start, String raw) {
  final before = text.substring(0, start);
  if (before.length >= 3) {
    final from = before.length - (before.length < 12 ? before.length : 12);
    if (before.substring(from).contains('://')) return true;
  }
  if (raw.startsWith('//') && before.endsWith(':')) return true;
  if (before.endsWith(':/')) return true;
  return false;
}

/// Splits [text] into plain segments and path-link segments. Pure and
/// synchronous, so it is unit-testable without any widget or API.
List<PathLinkSegment> extractPathLinks(String text) {
  if (!text.contains('/')) return [PathLinkSegment(text)];
  final hits = <({int start, String raw})>[];
// Absolute matches win; a relative match overlapping one is dropped.
  for (final pattern in [_absolutePattern, _relativePattern]) {
    for (final match in pattern.allMatches(text)) {
      final start = match.start;
      if (start > 0 && !_boundary.contains(text[start - 1])) continue;
      final raw = _cleanPath(match.group(1)!);
      if (raw.isEmpty || !_plausible(raw)) continue;
      if (_isUrl(text, start, raw)) continue;
      if (hits.any(
          (hit) => start >= hit.start && start < hit.start + hit.raw.length)) {
        continue;
      }
      hits.add((start: start, raw: raw));
    }
  }
  hits.sort((a, b) => a.start.compareTo(b.start));
  if (hits.isEmpty) return [PathLinkSegment(text)];
  final segments = <PathLinkSegment>[];
  var cursor = 0;
  for (final hit in hits) {
    if (hit.start < cursor) continue;
    if (hit.start > cursor) {
      segments.add(PathLinkSegment(text.substring(cursor, hit.start)));
    }
    segments.add(PathLinkSegment(hit.raw, path: hit.raw));
    cursor = hit.start + hit.raw.length;
  }
  if (cursor < text.length) {
    segments.add(PathLinkSegment(text.substring(cursor)));
  }
  return segments;
}

String humanSize(num bytes) {
  final n = bytes.toDouble();
  if (n < 1024) return '${n.toInt()} B';
  if (n < 1024 * 1024) {
    return '${(n / 1024).toStringAsFixed(n < 10240 ? 1 : 0)} KB';
  }
  return '${(n / 1024 / 1024).toStringAsFixed(1)} MB';
}

/// What a tapped path link needs from its host (ChatScreen). The host
/// owns the API and the navigation; this widget only detects, stats
/// once, and offers the Preview / Download choice.
class PathLinkActions {
  const PathLinkActions({
    required this.resolve,
    required this.onPreview,
    required this.onDownload,
    required this.onOpenDir,
  });

  /// One fileStat call for the raw (possibly relative) path; returns
  /// the server map {path, name, exists, kind, size, mtime, parent,
  /// offset} — the resolved absolute path is in `path`.
  final Future<Map<String, dynamic>> Function(String rawPath) resolve;

  /// Show a text preview of the resolved file. Text files reach here
  /// from the sheet's Preview button only, never from a bare tap.
  final void Function(String resolvedPath, Map<String, dynamic> stat) onPreview;

  /// Download the resolved file through the existing save flow. On
  /// touch devices the host confirms first (web downloads.js maybe()).
  final void Function(String resolvedPath, Map<String, dynamic> stat)
      onDownload;

  /// Open the resolved directory in the file panel.
  final void Function(String resolvedPath) onOpenDir;
}

/// Opens the Preview / Download sheet for [rawPath], stat'ing it once
/// (cached in [cache]) on first tap. Shared by [PathLinkText] and the
/// markdown path-link builder so both go through the same choice.
Future<void> openPathLinkSheet(
  BuildContext context,
  PathLinkActions actions,
  Map<String, Map<String, dynamic>> cache,
  String rawPath,
) async {
  var stat = cache[rawPath];
  if (stat == null) {
    try {
      stat = await actions.resolve(rawPath);
    } catch (error) {
      if (context.mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$error')));
      }
      return;
    }
    cache[rawPath] = stat;
    if (!context.mounted) return;
  }
  final resolved = '${stat['path'] ?? rawPath}';
  final name = '${stat['name'] ?? rawPath}';
  final exists = stat['exists'] == true;
  final kind = '${stat['kind'] ?? ''}';
  final isDir = kind == 'dir';
  final previewable = exists && !isDir && isTextPath(resolved);
  final meta = !exists
      ? '找不到这个路径'
      : isDir
          ? '目录'
          : '${kind == 'link' ? '符号链接 · ' : ''}${humanSize((stat['size'] as num?) ?? 0)}';
  await showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    builder: (sheetContext) => SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 8, 20, 20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(name,
                style:
                    const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
            const SizedBox(height: 4),
            Text(resolved,
                style: TextStyle(fontSize: 12, color: AppTheme.muted)),
            const SizedBox(height: 4),
            Text(meta, style: TextStyle(fontSize: 12, color: AppTheme.muted)),
            const SizedBox(height: 12),
            if (previewable)
              ListTile(
                key: const Key('path-link-preview'),
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.visibility_outlined, size: 20),
                title: const Text('预览'),
                onTap: () {
                  Navigator.of(sheetContext).pop();
                  actions.onPreview(resolved, stat!);
                },
              ),
            if (exists && !isDir)
              ListTile(
                key: const Key('path-link-download'),
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.download_outlined, size: 20),
                title: const Text('下载'),
                onTap: () {
                  Navigator.of(sheetContext).pop();
                  actions.onDownload(resolved, stat!);
                },
              ),
            if (exists && isDir)
              ListTile(
                key: const Key('path-link-open-dir'),
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.folder_open_outlined, size: 20),
                title: const Text('打开目录'),
                onTap: () {
                  Navigator.of(sheetContext).pop();
                  actions.onOpenDir(resolved);
                },
              ),
            if (!exists)
              const ListTile(
                contentPadding: EdgeInsets.zero,
                leading: Icon(Icons.error_outline, size: 20),
                title: Text('文件不存在，无法预览或下载'),
              ),
          ],
        ),
      ),
    ),
  );
}

/// Selectable text with tappable path links. Plain text renders exactly
/// like [SelectableText]; link spans are accent-coloured and underlined.
class PathLinkText extends StatefulWidget {
  const PathLinkText(
      {super.key, required this.text, required this.actions, this.style});

  final String text;
  final PathLinkActions actions;
  final TextStyle? style;

  @override
  State<PathLinkText> createState() => _PathLinkTextState();
}

class _PathLinkTextState extends State<PathLinkText> {
  List<PathLinkSegment> _segments = const [];
  List<TapGestureRecognizer> _recognizers = const [];
  final Map<String, Map<String, dynamic>> _cache = {};

  @override
  void initState() {
    super.initState();
    _rebuild();
  }

  @override
  void didUpdateWidget(PathLinkText oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.text != widget.text) _rebuild();
  }

  void _rebuild() {
    for (final recognizer in _recognizers) {
      recognizer.dispose();
    }
    _segments = extractPathLinks(widget.text);
    _recognizers = _segments.where((segment) => segment.isLink).map((segment) {
      final path = segment.path!;
      return TapGestureRecognizer()
        ..onTap =
            () => openPathLinkSheet(context, widget.actions, _cache, path);
    }).toList();
  }

  @override
  void dispose() {
    for (final recognizer in _recognizers) {
      recognizer.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final base = widget.style ?? const TextStyle(fontSize: 14.5, height: 1.45);
    var linkIndex = 0;
    return SelectableText.rich(
      TextSpan(
        children: _segments.map((segment) {
          if (!segment.isLink) {
            return TextSpan(text: segment.text, style: base);
          }
          final recognizer = _recognizers[linkIndex++];
          return TextSpan(
            text: segment.text,
            style: base.copyWith(
                color: AppTheme.accent, decoration: TextDecoration.underline),
            recognizer: recognizer,
          );
        }).toList(),
      ),
    );
  }
}

/// Markdown inline syntax for path links inside assistant text blocks,
/// so a path in rendered markdown is tappable without giving up the
/// markdown rendering itself. Code spans win over this syntax (the
/// parser consumes a backtick span before reaching the text inside),
/// matching web's rule of never touching <pre>/<code>.
class PathLinkSyntax extends md.InlineSyntax {
  PathLinkSyntax()
      : super(
            r'((?:~|\.{1,2})?\/[^\s\x22\x27\x60<>|\uFF0C\u3002\uFF1B\uFF1A\u3001\uFF09)\u3011]+|(?:\.{1,2}\/)?(?:[\w@%+.-]+\/)+[\w@%+.-]+\.[A-Za-z0-9]{1,8})');

  @override
  bool onMatch(md.InlineParser parser, Match match) {
    final raw = _cleanPath(match.group(1)!);
    final start = match.start;
    if (raw.isEmpty || !_plausible(raw)) {
      parser.addNode(md.Text(match.group(0)!));
      return true;
    }
// A path-constituent char directly before the match means this is
// the tail of a longer token, not a path start.
    if (start > 0 && _pathChar.hasMatch(match.input[start - 1])) {
      parser.addNode(md.Text(match.group(0)!));
      return true;
    }
    if (_isUrl(match.input, start, raw)) {
      parser.addNode(md.Text(match.group(0)!));
      return true;
    }
    parser.addNode(md.Element('plink', [md.Text(raw)]));
    return true;
  }
}

/// Renders a `plink` inline element as a tappable accent span that opens
/// the same Preview / Download sheet as [PathLinkText].
class PathLinkElementBuilder extends MarkdownElementBuilder {
  PathLinkElementBuilder({required this.actions, required this.cache});

  final PathLinkActions actions;
  final Map<String, Map<String, dynamic>> cache;

  @override
  Widget? visitElementAfterWithContext(
    BuildContext context,
    md.Element element,
    TextStyle? preferredStyle,
    TextStyle? parentStyle,
  ) {
    final raw = element.textContent;
    if (raw.isEmpty) return null;
    return GestureDetector(
      onTap: () => openPathLinkSheet(context, actions, cache, raw),
      child: Text(
        raw,
        style: (parentStyle ?? preferredStyle)?.copyWith(
            color: AppTheme.accent, decoration: TextDecoration.underline),
      ),
    );
  }
}
