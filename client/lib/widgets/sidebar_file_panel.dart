import 'dart:convert';

import 'package:desktop_drop/desktop_drop.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/api_client.dart';
import '../models/models.dart';
import '../services/drop_files.dart';
import '../services/save_file.dart';
import '../theme.dart';

/// One pane's sidebar view, as web stores it in
/// localStorage['tmux-files-view'] (commit 2c282a4, web/index.html):
/// `{open: bool, path: string}` per pane, most recent last, capped at
/// 60 panes.
///
/// Flutter's [SidebarViewMemory] (settings_screen.dart) stores the same
/// key as a `Map<String, String>` (pane ref → view string), so the web
/// object is carried as that string: [encode] JSON-encodes it, and
/// [tryParse] decodes it. The legacy plain scaffolding value
/// `files:/dir` (open panel, that directory) still parses, so rows
/// written before this panel existed are not mistaken for "no record".
class SidebarFileView {
  const SidebarFileView({required this.open, this.path = ''});

  final bool open;
  final String path;

  String encode() => jsonEncode({'open': open, 'path': path});

  static SidebarFileView? tryParse(String? raw) {
    if (raw == null || raw.isEmpty) return null;
    try {
      final data = jsonDecode(raw);
      if (data is Map) {
        final open = data['open'];
        if (open is bool) return SidebarFileView(open: open, path: '${data['path'] ?? ''}');
      }
    } catch (_) {
      // Not JSON: fall through to the legacy plain form below.
    }
    if (raw == 'files') return const SidebarFileView(open: true);
    if (raw.startsWith('files:')) {
      return SidebarFileView(open: true, path: raw.substring('files:'.length));
    }
    return null;
  }
}

/// The console sidebar's file panel (web's `#file-panel`, web/index.html):
/// the directory listing for one pane, shown in place of the session
/// tree while it is open. Browsing uses the same file APIs
/// [FilesScreen] uses ([Api.listFiles] / [Api.readFile]); no new server
/// endpoints.
///
/// The panel loads once in [initState] at [initialPath] (or the pane's
/// own directory when that is empty, like web's `openFiles(null)`), and
/// reports every resolved directory through [onDirectoryChanged] —
/// web writes the per-pane view a second time once the directory is
/// known (the 2c282a4 empty-path fix), and the hosts shell does the
/// same from that callback. A pane change remounts the panel (the shell
/// keys it by pane ref), so [didUpdateWidget] only reloads on a pane
/// change: a directory change reported back as a new [initialPath]
/// must not reload the panel that just reported it.
///
/// Actions mirror web's file panel (same web/index.html lines as
/// [FilesScreen]): the bar has zip-download and picker upload, rows
/// have checkboxes, and the footer offers select-all / clear / rename
/// / delete / zip with web's `syncFileSelection` visibility rules.
/// Upload, rename and delete copy [FilesScreen]'s dialog and message
/// shapes; OS drag-and-drop uses the same [DropTarget] seam and
/// upload path as [FilesScreen] (web's panel accepts drops too).
class SidebarFilePanel extends StatefulWidget {
  const SidebarFilePanel({
    super.key,
    required this.api,
    required this.pane,
    this.initialPath,
    required this.onClose,
    required this.onDirectoryChanged,
    this.pickUploads,
    this.hostLabel,
  });

  final Api api;
  final Pane pane;

  /// Owning host name for the footer chip (web's `#file-host`, set in
  /// `openFiles` from the host that owns the pane). The hosts shell
  /// passes the owner's name; when absent, the panel falls back to
  /// web's rule exactly: `remote` when the pane ref contains `~`
  /// (a remote pane), else `localhost`.
  final String? hostLabel;

  /// Directory to open first; empty/absent means the pane's directory.
  final String? initialPath;

  final VoidCallback onClose;

  /// Every directory the panel actually resolves to (after the listing
  /// returns, not the requested path — the requested one may be empty).
  final void Function(String path) onDirectoryChanged;

  /// Test seam for the upload picker: production passes null and the
  /// panel uses [FilePicker.pickFiles] exactly like [FilesScreen];
  /// tests inject picked (name, bytes) pairs so no real file IO runs
  /// inside a widget test.
  final Future<List<({String name, List<int> bytes})>> Function()? pickUploads;

  @override
  State<SidebarFilePanel> createState() => _SidebarFilePanelState();
}

class _SidebarFilePanelState extends State<SidebarFilePanel> {
  FileListing? _listing;
  String? _error;
  bool _loading = true;
  bool _busy = false;

  /// Desktop only, like [FilesScreen]: web's file panel accepts
  /// drops, but there is no OS drop target on mobile, where the
  /// picker button stays the only upload path.
  static bool get _isDesktop => const {
        TargetPlatform.macOS,
        TargetPlatform.windows,
        TargetPlatform.linux,
      }.contains(defaultTargetPlatform);

  /// True while an OS drag hovers the panel (web's
  /// `.file-panel.dragging` outline).
  bool _dragging = false;

  /// Monotonic request id: completions from an older directory request
  /// are ignored, like [FilesScreen]'s filesSeq guard.
  int _requestId = 0;

  /// Last directory requested, for the retry button.
  String _currentPath = '';

  /// Web's `#file-path` input (web/index.html): an editable path
  /// field, not a label. Its semantics, matched exactly:
  /// - focus puts the caret at the end (`setSelectionRange(len,len)`);
  /// - Enter commits (empty restores the current path instead),
  ///   then blurs;
  /// - Escape restores the current path, then blurs;
  /// - blur restores any uncommitted text — blur never commits.
  /// The field is re-synced from every successful listing, like
  /// web's `showPath(d.path)` on `openFiles` success — including
  /// the Enter-then-blur sequence, where the blur restore briefly
  /// shows the old path until the new listing lands.
  final _pathController = TextEditingController();
  late final _pathFocusNode = FocusNode(
    onKeyEvent: (node, event) {
      if (event is KeyDownEvent && event.logicalKey == LogicalKeyboardKey.escape) {
        _restorePathField();
        node.unfocus();
        return KeyEventResult.handled;
      }
      return KeyEventResult.ignored;
    },
  );

  /// Directory the field must show when it is not being edited:
  /// the last resolved listing, or empty before the first load
  /// (web's field is empty until the first `showPath` too).
  String get _resolvedPath => _listing?.path ?? '';

  void _syncPathField(String path) {
    _pathController.value = TextEditingValue(
      text: path,
      selection: TextSelection.collapsed(offset: path.length),
    );
  }

  void _restorePathField() => _syncPathField(_resolvedPath);

  void _handlePathFocusChange() {
    if (!_pathFocusNode.hasFocus) {
      // Blur restores uncommitted text (web's blur handler).
      if (_pathController.text != _resolvedPath) _restorePathField();
    } else {
      // Focus puts the caret at the end (web's focus handler).
      final text = _pathController.text;
      _pathController.selection = TextSelection.collapsed(offset: text.length);
    }
  }

  /// Web's `#file-host` chip text: the explicit [SidebarFilePanel.hostLabel]
  /// when the caller wired it, else web's openFiles fallback.
  String get _hostLabel => widget.hostLabel ?? (widget.pane.ref.contains('~') ? 'remote' : 'localhost');

  /// Paths picked with the per-row checkboxes (web's filesPicked).
  /// Cleared on every successful listing, like web's `openFiles`
  /// clearing filesPicked and [FilesScreen]'s `_open`.
  final Set<String> _selected = {};

  /// Row most recently interacted with, for the F2 / Delete
  /// shortcuts (web's list keydown reads the focused [data-entry]
  /// row) — set by row taps, checkbox toggles and menu opens, and
  /// cleared by tapping the `..` row, which has no data-entry.
  /// Like [FilesScreen]'s field of the same name.
  String? _focusedPath;

  @override
  void initState() {
    super.initState();
    _pathFocusNode.addListener(_handlePathFocusChange);
    _open(_initialDirectory);
  }

  @override
  void dispose() {
    _pathFocusNode.removeListener(_handlePathFocusChange);
    _pathFocusNode.dispose();
    _pathController.dispose();
    super.dispose();
  }

  @override
  void didUpdateWidget(SidebarFilePanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.pane.ref != oldWidget.pane.ref) _open(_initialDirectory);
  }

  String get _initialDirectory {
    final initial = widget.initialPath;
    if (initial != null && initial.isNotEmpty) return initial;
    return widget.pane.path;
  }

  Future<void> _open(String path) async {
    final requestId = ++_requestId;
    _currentPath = path;
    setState(() => _loading = true);
    try {
      final listing = await widget.api.listFiles(widget.pane.ref, path: path);
      if (!mounted || requestId != _requestId) return;
      setState(() {
        _listing = listing;
        _error = null;
        _loading = false;
        _selected.clear();
      });
      _syncPathField(listing.path);
      widget.onDirectoryChanged(listing.path);
    } catch (error) {
      if (!mounted || requestId != _requestId) return;
      setState(() {
        _error = '$error';
        _loading = false;
      });
    }
  }

  Future<void> _save(List<int> bytes, String name) async {
    final saved = await saveFileBytes(name, bytes);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('已保存到 $saved')));
  }

  Future<void> _download(FileEntry entry) async {
    setState(() => _busy = true);
    try {
      final bytes = await widget.api.readFile(widget.pane.ref, entry.path);
      await _save(bytes, entry.name);
    } catch (error) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// Zip one directory row, like web's file-menu '打包下载' and
  /// [FilesScreen]'s `_zipDirectory`.
  Future<void> _zipDirectory(FileEntry entry) async {
    final listing = _listing;
    if (listing == null) return;
    setState(() => _busy = true);
    try {
      final bytes = await widget.api.zipFiles(widget.pane.ref, [entry.path], listing.path);
      await _save(bytes, '${entry.name}.zip');
    } catch (error) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// Web's file menu (setupFileMenu/showFileMenu), via showMenu like
  /// [FilesScreen]: right-click or long-press opens a menu
  /// positioned at the pointer, clamped to the viewport like web's
  /// max(6, min(x, innerWidth - width - 8)) rule. Item set exactly:
  /// 打开 (dirs only), 下载 / 打包下载 (dirs), 重命名, 删除.
  /// Escape and barrier taps dismiss (showMenu built-ins, matching
  /// web's Escape/pointerdown dismiss). Every action reuses the
  /// panel's existing download/rename/delete, plus [_zipDirectory]
  /// for a dir row. Long-press timing is the framework default
  /// (~500ms) against web's 520ms timer — the closest Flutter
  /// offers without a hand-rolled recognizer.
  Future<void> _showContextMenu(FileEntry entry, Offset globalPosition) async {
    _focusedPath = entry.path;
    final size = MediaQuery.of(context).size;
    const menuWidth = 160.0;
    const menuHeight = 180.0;
    final dx = globalPosition.dx.clamp(6.0, (size.width - menuWidth - 8).clamp(6.0, size.width));
    final dy = globalPosition.dy.clamp(6.0, (size.height - menuHeight - 8).clamp(6.0, size.height));
    final action = await showMenu<String>(
      context: context,
      position: RelativeRect.fromLTRB(dx, dy, size.width - dx, size.height - dy),
      items: [
        if (entry.isDir)
          const PopupMenuItem(
            key: Key('sidebar-file-menu-open'),
            value: 'open',
            child: Text('打开'),
          ),
        PopupMenuItem(
          key: entry.isDir ? const Key('sidebar-file-menu-zipdir') : const Key('sidebar-file-menu-download'),
          value: entry.isDir ? 'zipdir' : 'download',
          child: Text(entry.isDir ? '打包下载' : '下载'),
        ),
        const PopupMenuItem(key: Key('sidebar-file-menu-rename'), value: 'rename', child: Text('重命名')),
        PopupMenuItem(
          key: const Key('sidebar-file-menu-delete'),
          value: 'delete',
          child: Text('删除', style: TextStyle(color: AppTheme.danger)),
        ),
      ],
    );
    if (!mounted || action == null) return;
    switch (action) {
      case 'open':
        _open(entry.path);
      case 'download':
        _download(entry);
      case 'zipdir':
        _zipDirectory(entry);
      case 'rename':
        _rename(entry);
      case 'delete':
        _deleteEntries([entry]);
    }
  }

  /// Zip the checkbox selection, or the current directory when
  /// nothing is selected (web's `file-download` handler zips the
  /// picked paths, else the current directory; Flutter expresses the
  /// latter as an empty path list plus the directory base, exactly
  /// like [FilesScreen]'s `_zipCurrent`, per the [Api.zipFiles]
  /// contract). The bar tooltip follows web's `syncFileSelection`
  /// title rule: picked ? 打包下载选中的 N 项 : 打包下载当前目录.
  Future<void> _zip() async {
    final listing = _listing;
    if (listing == null) return;
    setState(() => _busy = true);
    try {
      final bytes = await widget.api.zipFiles(widget.pane.ref, _selected.toList(), listing.path);
      final name = '${listing.path.split('/').where((part) => part.isNotEmpty).lastOrNull ?? 'files'}.zip';
      await _save(bytes, name);
    } catch (error) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<List<({String name, List<int> bytes})>> _defaultPickUploads() async {
    // Web's file input is multiple: upload every picked file, not
    // just the first — same shape as [FilesScreen]'s `_upload`.
    final picked = await FilePicker.pickFiles();
    final uploads = <({String name, List<int> bytes})>[];
    for (final file in picked) {
      uploads.add((name: file.name, bytes: await file.xFile.readAsBytes()));
    }
    return uploads;
  }

  Future<void> _upload() async {
    final picked = await (widget.pickUploads ?? _defaultPickUploads)();
    if (picked.isEmpty) return;
    await _uploadAll(picked);
  }

  /// OS drop on desktop, through the same upload path as the picker
  /// and the same shared [readDroppedUploads] seam as [FilesScreen].
  Future<void> _handleDrop(DropDoneDetails details) async {
    if (mounted) setState(() => _dragging = false);
    final listing = _listing;
    if (listing == null || _busy) return;
    final dropped = await readDroppedUploads(details);
    if (dropped.failure != null) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('${dropped.failure!.name}: ${dropped.failure!.error}')),
        );
      }
      return;
    }
    if (dropped.files.isEmpty) return;
    await _uploadAll(dropped.files);
  }

  /// Wraps the panel in a [DropTarget] on desktop only, covering
  /// the whole panel (path bar, list, selection bar), like web's
  /// `#file-panel` drop listeners and [FilesScreen]'s wrapper.
  Widget _dropWrap(Widget child) {
    if (!_isDesktop) return child;
    return DropTarget(
      key: const Key('sidebar-files-drop-target'),
      onDragEntered: (_) {
        if (mounted) setState(() => _dragging = true);
      },
      onDragExited: (_) {
        if (mounted) setState(() => _dragging = false);
      },
      onDragDone: _handleDrop,
      child: Stack(
        children: [
          child,
          if (_dragging) _dropOverlay(),
        ],
      ),
    );
  }

  /// Web's `.file-panel.dragging` affordance, same as [FilesScreen]'s
  /// overlay: a 2px dashed accent outline plus a drop hint.
  /// IgnorePointer so the overlay itself can never swallow a tap if
  /// the drag state ever goes stale.
  Widget _dropOverlay() {
    return Positioned.fill(
      child: IgnorePointer(
        child: Container(
          key: const Key('sidebar-files-drop-overlay'),
          color: AppTheme.accent.withValues(alpha: 0.08),
          child: CustomPaint(
            painter: DashedDropBorderPainter(AppTheme.accent),
            child: Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.file_upload_outlined, size: 32, color: AppTheme.accent),
                  const SizedBox(height: 8),
                  Text('松开以上传到当前目录', style: TextStyle(fontSize: 14, color: AppTheme.accent)),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  /// Same `uploadFile` calls, same refresh, same messages as web's
  /// `uploadFiles` and [FilesScreen]'s `_uploadAll`: success always
  /// reports the count (`已上传 N 个文件`), and the first failure
  /// stops the batch with `<name>: <error>`, exactly like web.
  Future<void> _uploadAll(List<({String name, List<int> bytes})> uploads) async {
    final listing = _listing;
    if (listing == null || uploads.isEmpty) return;
    setState(() => _busy = true);
    var current = '';
    try {
      for (final upload in uploads) {
        current = upload.name;
        await widget.api.uploadFile(widget.pane.ref, listing.path, upload.name, upload.bytes);
      }
      await _open(listing.path);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('已上传 ${uploads.length} 个文件')));
      }
    } catch (error) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$current: $error')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// Web's `renameFile`: a dialog prefilled with the current name
  /// (stem selected, extension left alone), then refresh. Only ever
  /// offered for exactly one selection, like web's footer rename.
  Future<void> _rename(FileEntry entry) async {
    final dot = entry.name.lastIndexOf('.');
    final controller = TextEditingController(text: entry.name)
      ..selection = TextSelection(baseOffset: 0, extentOffset: dot > 0 ? dot : entry.name.length);
    final name = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('重命名'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(entry.path, style: TextStyle(fontSize: 12, color: AppTheme.muted)),
            const SizedBox(height: 10),
            TextField(
              key: const Key('sidebar-file-rename-input'),
              controller: controller,
              autofocus: true,
              // 16px: web's mobile input rule — smaller text triggers
              // iOS auto-zoom on focus (same as FilesScreen).
              style: const TextStyle(fontSize: 16),
              onSubmitted: (value) => Navigator.pop(context, value.trim()),
              decoration: const InputDecoration(labelText: '新名字'),
            ),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('取消')),
          FilledButton(
            key: const Key('sidebar-file-rename-ok'),
            onPressed: () => Navigator.pop(context, controller.text.trim()),
            child: const Text('重命名'),
          ),
        ],
      ),
    );
    if (!mounted || name == null || name.isEmpty || name == entry.name) return;
    try {
      await widget.api.renameFile(widget.pane.ref, entry.path, name);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('已重命名为 $name')));
      await _open(_listing?.path ?? widget.pane.path);
    } catch (error) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('重命名失败：$error')));
    }
  }

  /// Delete one row or the whole checkbox selection, like web's
  /// `deleteFiles` and [FilesScreen]'s `_deleteEntries`: the dialog
  /// previews at most 5 names plus '等 N 项', and the recursive
  /// checkbox appears only when a directory is selected, defaulting
  /// to on in that case (FilesScreen's rule for dir rows).
  Future<void> _deleteEntries(List<FileEntry> entries) async {
    if (entries.isEmpty) return;
    final names = entries.map((entry) => entry.name).toList();
    final preview = names.take(5).join('、') + (names.length > 5 ? ' 等 ${names.length} 项' : '');
    var recursive = entries.any((entry) => entry.isDir);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: Text(entries.length > 1 ? '删除 ${entries.length} 项' : '删除'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(preview, style: const TextStyle(fontWeight: FontWeight.w600)),
              const SizedBox(height: 4),
              Text('删除后无法恢复。', style: TextStyle(fontSize: 12, color: AppTheme.danger)),
              if (entries.any((entry) => entry.isDir))
                CheckboxListTile(
                  key: const Key('sidebar-file-delete-recursive'),
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  value: recursive,
                  onChanged: (value) => setDialogState(() => recursive = value ?? false),
                  title: const Text('连同目录内容一起删除', style: TextStyle(fontSize: 13)),
                ),
            ],
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('取消')),
            FilledButton(
              key: const Key('sidebar-file-delete-ok'),
              style: FilledButton.styleFrom(backgroundColor: AppTheme.danger),
              onPressed: () => Navigator.pop(context, true),
              child: const Text('删除'),
            ),
          ],
        ),
      ),
    );
    if (!mounted || confirmed != true) return;
    try {
      await widget.api.deleteFiles(widget.pane.ref, entries.map((entry) => entry.path).toList(), recursive: recursive);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(entries.length == 1 ? '已删除 ${entries.first.name}' : '已删除 ${entries.length} 项'),
      ));
      _selected.removeAll(entries.map((entry) => entry.path));
      await _open(_listing?.path ?? widget.pane.path);
    } catch (error) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('删除失败：$error')));
    }
  }

  FileEntry? _entryForPath(String path) {
    final entries = _listing?.entries;
    if (entries == null) return null;
    for (final entry in entries) {
      if (entry.path == path) return entry;
    }
    return null;
  }

  void _toggleSelected(FileEntry entry, bool selected) {
    setState(() {
      _focusedPath = entry.path;
      if (selected) {
        _selected.add(entry.path);
      } else {
        _selected.remove(entry.path);
      }
    });
  }

  /// F2 renames the single selection, else the focused row — web's
  /// file-list keydown rule (picked.length == 1 ? picked[0] :
  /// focused), same as [FilesScreen]'s shortcut.
  void _shortcutRename() {
    if (_busy || _pathFocusNode.hasFocus) return;
    FileEntry? entry;
    if (_selected.length == 1) {
      entry = _entryForPath(_selected.first);
    } else if (_focusedPath != null) {
      entry = _entryForPath(_focusedPath!);
    }
    if (entry != null) _rename(entry);
  }

  /// Delete deletes the selection, else the focused row (web rule).
  void _shortcutDelete() {
    if (_busy || _pathFocusNode.hasFocus) return;
    final entries = _selected.isNotEmpty
        ? _selected.map(_entryForPath).whereType<FileEntry>().toList()
        : (_focusedPath == null ? const <FileEntry>[] : [_entryForPath(_focusedPath!)].whereType<FileEntry>().toList());
    if (entries.isNotEmpty) _deleteEntries(entries);
  }

  @override
  Widget build(BuildContext context) {
    final listing = _listing;
    // Web's syncFileSelection title rule for the bar download button.
    final zipTooltip = _selected.isEmpty ? '打包下载当前目录' : '打包下载选中的 ${_selected.length} 项';
    // Web's file-list keydown, scoped to this panel exactly like
    // [FilesScreen]: the shortcuts resolve only while focus is
    // inside the panel subtree (the autofocus Focus below), and are
    // suppressed while the path field has focus (web's listener is
    // on #file-list, not on the path input).
    return _dropWrap(CallbackShortcuts(
      bindings: {
        const SingleActivator(LogicalKeyboardKey.f2): _shortcutRename,
        const SingleActivator(LogicalKeyboardKey.delete): _shortcutDelete,
      },
      child: Focus(
        autofocus: true,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 8, 6),
          child: Row(
            children: [
              Expanded(
                child: TextField(
                  key: const Key('sidebar-files-path'),
                  controller: _pathController,
                  focusNode: _pathFocusNode,
                  autocorrect: false,
                  style: AppTheme.mono,
                  textInputAction: TextInputAction.go,
                  // Web's keydown handler: Enter commits (empty
                  // restores), then blurs — the blur restore below
                  // briefly shows the old path until this listing
                  // lands, exactly like web.
                  onSubmitted: (value) {
                    final trimmed = value.trim();
                    if (trimmed.isEmpty) {
                      _restorePathField();
                    } else {
                      _open(trimmed);
                    }
                    _pathFocusNode.unfocus();
                  },
                  decoration: InputDecoration(
                    isDense: true,
                    contentPadding: const EdgeInsets.symmetric(horizontal: 6, vertical: 8),
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(6),
                      borderSide: BorderSide(color: AppTheme.line),
                    ),
                    enabledBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(6),
                      borderSide: BorderSide(color: AppTheme.line),
                    ),
                    focusedBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(6),
                      borderSide: BorderSide(color: AppTheme.accent),
                    ),
                  ),
                ),
              ),
              IconButton(
                key: const Key('sidebar-files-zip'),
                tooltip: zipTooltip,
                iconSize: 18,
                visualDensity: VisualDensity.compact,
                onPressed: _busy ? null : _zip,
                icon: const Icon(Icons.folder_zip_outlined, size: 18),
              ),
              IconButton(
                key: const Key('sidebar-files-upload'),
                tooltip: '上传文件',
                iconSize: 18,
                visualDensity: VisualDensity.compact,
                onPressed: _busy ? null : _upload,
                icon: const Icon(Icons.upload_file, size: 18),
              ),
              IconButton(
                key: const Key('sidebar-files-up'),
                tooltip: '上级目录',
                iconSize: 18,
                visualDensity: VisualDensity.compact,
                onPressed: listing?.parent == null ? null : () => _open(listing!.parent!),
                icon: const Icon(Icons.arrow_upward, size: 18),
              ),
              IconButton(
                key: const Key('sidebar-files-refresh'),
                tooltip: '刷新',
                iconSize: 18,
                visualDensity: VisualDensity.compact,
                onPressed: () => _open(_currentPath),
                icon: const Icon(Icons.refresh, size: 18),
              ),
              IconButton(
                key: const Key('sidebar-files-close'),
                tooltip: '隐藏文件面板',
                iconSize: 18,
                visualDensity: VisualDensity.compact,
                onPressed: widget.onClose,
                icon: const Icon(Icons.close, size: 18),
              ),
            ],
          ),
        ),
        if (_busy) const LinearProgressIndicator(minHeight: 2),
        const Divider(height: 1),
        Expanded(child: _buildList(listing)),
        if (_selected.isNotEmpty) _buildSelectionBar(),
        _buildFooter(),
          ],
        ),
      ),
    ));
  }

  /// Web's `#file-status` texts, in openFiles/setFileStatus order:
  /// loading always says `读取目录…` (even over a stale listing, like
  /// web setting it before every fetch); a failed fetch shows the
  /// error; a listing shows `N 项（已截断）· 拖拽文件可上传` (the
  /// parenthetical only when truncated); idle falls back to web's
  /// setFileStatus default `拖拽文件到此处上传`.
  String get _statusText {
    if (_loading) return '读取目录…';
    if (_error != null) return _error!;
    final listing = _listing;
    if (listing != null) {
      return '${listing.entries.length} 项${listing.truncated ? '（已截断）' : ''} · 拖拽文件可上传';
    }
    return '拖拽文件到此处上传';
  }

  /// Web's `.file-foot`: host chip plus the persistent status line.
  /// The chip is hidden until the first listing lands, like web's
  /// `#file-host` (hidden until openFiles sets it). The status line
  /// replaces the old list tail: web has no truncated tail row, the
  /// truncation note lives only in this footer text.
  Widget _buildFooter() {
    return Container(
      padding: const EdgeInsets.fromLTRB(8, 6, 8, 8),
      decoration: BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
      child: Row(
        children: [
          if (_listing != null)
            Container(
              key: const Key('sidebar-files-host'),
              constraints: const BoxConstraints(maxWidth: 76),
              margin: const EdgeInsets.only(right: 6),
              padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
              decoration: BoxDecoration(
                border: Border.all(color: AppTheme.line),
                borderRadius: BorderRadius.circular(5),
              ),
              child: Text(
                _hostLabel,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 10, fontFamily: 'monospace'),
              ),
            ),
          Expanded(
            child: Text(
              _statusText,
              key: const Key('sidebar-files-status'),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: 11, color: AppTheme.muted),
            ),
          ),
        ],
      ),
    );
  }

  /// Footer for the checkbox selection, with web's `syncFileSelection`
  /// visibility rules exactly: select-all is shown whenever anything
  /// is picked and flips to 取消全选 when all rows are picked; clear
  /// is hidden when all are picked (select-all already undoes it);
  /// rename only for exactly one pick; delete and zip whenever any
  /// rows are picked, with the zip label carrying the count like
  /// web's `下载 zip (N)`. Chip-style small buttons, horizontally
  /// scrollable so the bar survives the 280px sidebar width.
  Widget _buildSelectionBar() {
    final entries = _listing?.entries ?? const <FileEntry>[];
    final allSelected = entries.isNotEmpty && _selected.length == entries.length;
    final selectedEntries = _selected.map(_entryForPath).whereType<FileEntry>().toList();
    return Container(
      padding: const EdgeInsets.fromLTRB(8, 6, 8, 8),
      decoration: BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextButton(
              key: const Key('sidebar-files-select-all'),
              style: _chipStyle,
              onPressed: () => setState(() {
                if (allSelected) {
                  _selected.clear();
                } else {
                  _selected.addAll(entries.map((entry) => entry.path));
                }
              }),
              child: Text(allSelected ? '取消全选' : '全选'),
            ),
            if (!allSelected)
              TextButton(
                key: const Key('sidebar-files-select-none'),
                style: _chipStyle,
                onPressed: () => setState(_selected.clear),
                child: const Text('清空'),
              ),
            if (_selected.length == 1)
              TextButton(
                key: const Key('sidebar-files-footer-rename'),
                style: _chipStyle,
                onPressed: selectedEntries.isEmpty ? null : () => _rename(selectedEntries.first),
                child: const Text('重命名'),
              ),
            TextButton(
              key: const Key('sidebar-files-footer-delete'),
              style: _chipStyle,
              onPressed: () => _deleteEntries(selectedEntries),
              child: Text('删除', style: TextStyle(color: AppTheme.danger)),
            ),
            // OutlinedButton, not FilledButton: the app theme gives
            // FilledButton an infinite minimum width, which only lays
            // out when the button fills its parent — in a Row with
            // siblings it asserts (same reason as FilesScreen).
            OutlinedButton(
              key: const Key('sidebar-files-footer-zip'),
              style: OutlinedButton.styleFrom(
                padding: const EdgeInsets.symmetric(horizontal: 8),
                minimumSize: const Size(0, 28),
                tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                textStyle: const TextStyle(fontSize: 12),
              ),
              onPressed: _busy ? null : _zip,
              child: Text('下载 zip (${_selected.length})'),
            ),
          ],
        ),
      ),
    );
  }

  /// Web's `.chip-btn`: small, tight, no full-width tap target.
  static final ButtonStyle _chipStyle = TextButton.styleFrom(
    padding: const EdgeInsets.symmetric(horizontal: 8),
    minimumSize: const Size(0, 28),
    tapTargetSize: MaterialTapTargetSize.shrinkWrap,
    textStyle: const TextStyle(fontSize: 12),
  );

  Widget _buildList(FileListing? listing) {
    if (_loading && listing == null) return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    if (listing == null) {
      return ListView(
        padding: const EdgeInsets.all(24),
        children: [
          Text(_error ?? '加载失败', style: TextStyle(color: AppTheme.danger, fontSize: 13)),
          const SizedBox(height: 12),
          OutlinedButton(onPressed: () => _open(_currentPath), child: const Text('重试')),
        ],
      );
    }
    final entries = listing.entries;
    final parent = listing.parent;
    if (entries.isEmpty && parent == null) {
      return Center(child: Text('这个目录是空的', style: TextStyle(color: AppTheme.muted)));
    }
    return ListView.builder(
      itemCount: entries.length + (parent == null ? 0 : 1),
      itemBuilder: (context, index) {
        // Web renderFiles puts the `..` row first whenever a parent
        // exists. It has no checkbox and no context menu (web's menu
        // binds only [data-entry] rows); a modified click does
        // nothing, like web's `if(!(ctrl||meta||shift))
        // openFiles(dir)` guard. The F6 shortcuts likewise ignore it
        // (web's list keydown reads [data-entry] rows only).
        if (parent != null && index == 0) {
          return Material(
            type: MaterialType.transparency,
            child: ListTile(
              key: const Key('sidebar-file-parent'),
              dense: true,
              leading: Icon(Icons.subdirectory_arrow_left, size: 20, color: AppTheme.accent),
              title: const Text('..', maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 13.5)),
              onTap: () {
                // The `..` row is not a [data-entry] row, so in web a
                // click on it moves focus there and the keydown
                // handler finds no focused entry afterwards.
                setState(() => _focusedPath = null);
                final keys = HardwareKeyboard.instance;
                if (!keys.isShiftPressed && !keys.isControlPressed && !keys.isMetaPressed) {
                  _open(parent);
                }
              },
            ),
          );
        }
        final entry = entries[parent == null ? index : index - 1];
        final selected = _selected.contains(entry.path);
        // Transparent Material per row: the sidebar container paints a
        // background above the Scaffold's Material, which would hide
        // the ListTile ink (Flutter asserts on exactly this).
        // The GestureDetector adds web's context menu without
        // touching the primary tap: right-click and long-press open
        // the menu, and the long-press recognizer wins the gesture
        // arena over the ListTile tap, so a long-press never also
        // navigates/downloads.
        return GestureDetector(
          onSecondaryTapDown: (details) => _showContextMenu(entry, details.globalPosition),
          onLongPressStart: (details) => _showContextMenu(entry, details.globalPosition),
          child: Material(
          type: MaterialType.transparency,
          child: Row(
            children: [
              Checkbox(
                key: Key('sidebar-file-check-${entry.name}'),
                value: selected,
                visualDensity: VisualDensity.compact,
                materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                onChanged: (value) => _toggleSelected(entry, value ?? false),
              ),
              Expanded(
                child: ListTile(
                  key: Key('sidebar-file-${entry.name}'),
                  dense: true,
                  tileColor: selected ? AppTheme.accent.withValues(alpha: 0.08) : null,
                  leading: Icon(
                    entry.isDir ? Icons.folder_outlined : Icons.insert_drive_file_outlined,
                    size: 20,
                    color: entry.isDir ? AppTheme.accent : AppTheme.faint,
                  ),
                  title: Text(entry.name, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13.5)),
                  subtitle: entry.isDir ? null : Text(entry.sizeLabel, style: TextStyle(fontSize: 11, color: AppTheme.faint)),
                  trailing: entry.isDir
                      ? null
                      : IconButton(
                          key: Key('sidebar-file-download-${entry.name}'),
                          tooltip: '下载',
                          iconSize: 18,
                          visualDensity: VisualDensity.compact,
                          onPressed: _busy ? null : () => _download(entry),
                          icon: const Icon(Icons.download_outlined, size: 18),
                        ),
                  onTap: () => entry.isDir ? _open(entry.path) : _download(entry),
                ),
              ),
            ],
          ),
          ),
        );
      },
    );
  }
}
