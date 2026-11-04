import 'dart:convert';

import 'package:desktop_drop/desktop_drop.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import '../api/api_client.dart';
import '../services/save_file.dart';
import '../models/models.dart';
import '../theme.dart';

/// File panel for one pane: browse, preview, download, upload and zip.
class FilesScreen extends StatefulWidget {
  const FilesScreen({super.key, required this.api, required this.pane, this.initialPath});

  final Api api;
  final Pane pane;

  /// Directory to open first (chat path-link "打开目录"); defaults to
  /// the pane's working directory when absent or empty.
  final String? initialPath;

  @override
  State<FilesScreen> createState() => _FilesScreenState();
}

class _FilesScreenState extends State<FilesScreen> {
  final _pathController = TextEditingController();
  final _pathFocusNode = FocusNode();
  FileListing? _listing;
  String? _error;
  bool _loading = true;
  bool _busy = false;

  /// Monotonic request id: completions from an older directory request
  /// are ignored, like web's filesSeq guard (commit b22f23c).
  int _requestId = 0;

  /// Paths picked with the per-row checkboxes (web's filesPicked).
  final Set<String> _selected = {};

  /// True while an OS drag hovers the panel on desktop: web adds
  /// `.dragging` (2px dashed accent outline) to the file panel for
  /// exactly this state.
  bool _dragging = false;

  /// Desktop only: web's file panel accepts drops, but there is no
  /// OS drop target on mobile, where the picker button stays the
  /// only upload path.
  static bool get _isDesktop => const {
        TargetPlatform.macOS,
        TargetPlatform.windows,
        TargetPlatform.linux,
      }.contains(defaultTargetPlatform);

  /// Row most recently interacted with, for the F2 / Delete shortcuts.
  String? _focusedPath;

  @override
  void initState() {
    super.initState();
    final initial = widget.initialPath;
    _open(initial != null && initial.isNotEmpty ? initial : widget.pane.path);
  }

  @override
  void dispose() {
    _pathController.dispose();
    _pathFocusNode.dispose();
    super.dispose();
  }

  Future<void> _open(String path) async {
    final requestId = ++_requestId;
    setState(() => _loading = true);
    try {
      final listing = await widget.api.listFiles(widget.pane.ref, path: path);
      if (!mounted || requestId != _requestId) return;
      setState(() {
        _listing = listing;
        _pathController.text = listing.path;
        _error = null;
        _loading = false;
        _selected.clear();
      });
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

  Future<void> _zipCurrent() async {
    final listing = _listing;
    if (listing == null) return;
    setState(() => _busy = true);
    try {
      final bytes = await widget.api.zipFiles(widget.pane.ref, const [], listing.path);
      final name = '${listing.path.split('/').where((part) => part.isNotEmpty).lastOrNull ?? 'files'}.zip';
      await _save(bytes, name);
    } catch (error) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// Zip one directory row, like web's file-menu '打包下载'.
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

  /// Zip the checkbox selection, like web's footer '下载 zip'.
  Future<void> _zipSelection() async {
    final listing = _listing;
    if (listing == null || _selected.isEmpty) return;
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

  Future<void> _upload() async {
    final picked = await FilePicker.pickFiles();
    if (picked.isEmpty) return;
    // Web's file input is multiple: upload every picked file, not
    // just the first.
    final uploads = <({String name, List<int> bytes})>[];
    for (final file in picked) {
      uploads.add((name: file.name, bytes: await file.xFile.readAsBytes()));
    }
    await _uploadAll(uploads);
  }

  /// Shared upload path for the picker button and OS drops: same
  /// `uploadFile` calls, same refresh, same messages as web's
  /// `uploadFiles` — success always reports the count
  /// (`已上传 N 个文件`), and the first failure stops the batch
  /// with `<name>: <error>`, exactly like web.
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

  /// OS drop on desktop, through the same upload path as the picker.
  /// Directories arrive as [DropItemDirectory] (desktop_drop fills
  /// `children` only for the web drop path; the macOS plugin reports
  /// a directory with empty children), so a dropped folder on its
  /// own uploads nothing — exactly like web, whose drop handler
  /// only reads `dataTransfer.files`. Entries are read in drop
  /// order; the first unreadable entry stops the drop with
  /// `<name>: <error>`, matching web's `uploadFiles`, which returns
  /// on the first failure instead of continuing with the rest.
  Future<void> _handleDrop(DropDoneDetails details) async {
    if (mounted) setState(() => _dragging = false);
    final listing = _listing;
    if (listing == null || _busy) return;
    final files = <DropItem>[];
    void collect(DropItem item) {
      if (item is DropItemDirectory) {
        for (final child in item.children) {
          collect(child);
        }
      } else {
        files.add(item);
      }
    }

    for (final item in details.files) {
      collect(item);
    }
    if (files.isEmpty) return;
    final uploads = <({String name, List<int> bytes})>[];
    for (final file in files) {
      final bookmark = file.extraAppleBookmark;
      var started = false;
      try {
        if (bookmark != null && bookmark.isNotEmpty) {
          started = await DesktopDrop.instance.startAccessingSecurityScopedResource(bookmark: bookmark);
        }
        final bytes = await file.readAsBytes();
        var name = file.name.isNotEmpty ? file.name : file.path.split('/').last;
        if (name.isEmpty) name = '未知文件';
        uploads.add((name: name, bytes: bytes));
      } catch (error) {
        // Web's uploadFiles returns on the first failure; a drop
        // entry that cannot even be read stops the same way.
        if (mounted) {
          var name = file.name.isNotEmpty ? file.name : file.path.split('/').last;
          if (name.isEmpty) name = '未知文件';
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$name: $error')));
        }
        return;
      } finally {
        if (started) {
          await DesktopDrop.instance.stopAccessingSecurityScopedResource(bookmark: bookmark!);
        }
      }
    }
    await _uploadAll(uploads);
  }

  /// Wraps the panel body in a [DropTarget] on desktop only. The
  /// target covers the whole panel (path bar, list, selection bar),
  /// like web's `#file-panel` drop listeners; the AppBar stays
  /// outside it, as web's panel excludes the console topbar.
  Widget _dropWrap(Widget child) {
    if (!_isDesktop) return child;
    return DropTarget(
      key: const Key('files-drop-target'),
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

  /// Web's `.file-panel.dragging` affordance: a 2px dashed accent
  /// outline over the panel plus a drop hint. IgnorePointer so the
  /// overlay itself can never swallow a tap if the drag state
  /// ever goes stale.
  Widget _dropOverlay() {
    return Positioned.fill(
      child: IgnorePointer(
        child: Container(
          key: const Key('files-drop-overlay'),
          color: AppTheme.accent.withValues(alpha: 0.08),
          child: CustomPaint(
            painter: _DashedBorderPainter(AppTheme.accent),
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


  /// One action dispatch shared by the long-press sheet, the row
  /// '更多' button and the right-click menu, so all three offer the
  /// same items.
  Future<void> _doAction(FileEntry entry, String action) async {
    switch (action) {
      case 'open':
        entry.isDir ? await _open(entry.path) : await _preview(entry);
      case 'download':
        await _download(entry);
      case 'zipdir':
        await _zipDirectory(entry);
      case 'rename':
        await _rename(entry);
      case 'delete':
        await _deleteEntries([entry]);
    }
  }

  /// Right-click menu, clamped to the viewport like web's showFileMenu
  /// (max(6, min(x, innerWidth - width - 8))).
  Future<void> _showContextMenu(FileEntry entry, Offset globalPosition) async {
    _focusedPath = entry.path;
    final size = MediaQuery.of(context).size;
    const menuWidth = 200.0;
    const menuHeight = 260.0;
    final dx = globalPosition.dx.clamp(6.0, (size.width - menuWidth - 8).clamp(6.0, size.width));
    final dy = globalPosition.dy.clamp(6.0, (size.height - menuHeight - 8).clamp(6.0, size.height));
    final action = await showMenu<String>(
      context: context,
      position: RelativeRect.fromLTRB(dx, dy, size.width - dx, size.height - dy),
      items: [
        PopupMenuItem(
          key: const Key('file-context-open'),
          value: 'open',
          child: Text(entry.isDir ? '打开目录' : '预览'),
        ),
        PopupMenuItem(
          key: entry.isDir ? const Key('file-context-zipdir') : const Key('file-context-download'),
          value: entry.isDir ? 'zipdir' : 'download',
          child: Text(entry.isDir ? '打包下载' : '下载'),
        ),
        const PopupMenuItem(key: Key('file-context-rename'), value: 'rename', child: Text('重命名')),
        PopupMenuItem(
          key: const Key('file-context-delete'),
          value: 'delete',
          child: Text('删除', style: TextStyle(color: AppTheme.danger)),
        ),
      ],
    );
    if (!mounted || action == null) return;
    await _doAction(entry, action);
  }

  /// 行尾「更多」和长按共用：桌面右键的习惯在触屏上是长按。
  Future<void> _actions(FileEntry entry) async {
    final action = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: Icon(entry.isDir ? Icons.folder_open_outlined : Icons.open_in_new, size: 20),
              title: Text(entry.isDir ? '打开目录' : '预览'),
              onTap: () => Navigator.pop(context, 'open'),
            ),
            ListTile(
              key: entry.isDir ? const Key('file-action-zipdir') : const Key('file-action-download'),
              leading: const Icon(Icons.download_outlined, size: 20),
              title: Text(entry.isDir ? '打包下载' : '下载'),
              onTap: () => Navigator.pop(context, entry.isDir ? 'zipdir' : 'download'),
            ),
            const Divider(height: 1),
            ListTile(
              key: const Key('file-action-rename'),
              leading: const Icon(Icons.drive_file_rename_outline, size: 20),
              title: const Text('重命名'),
              onTap: () => Navigator.pop(context, 'rename'),
            ),
            ListTile(
              key: const Key('file-action-delete'),
              leading: Icon(Icons.delete_outline, size: 20, color: AppTheme.danger),
              title: Text('删除', style: TextStyle(color: AppTheme.danger)),
              onTap: () => Navigator.pop(context, 'delete'),
            ),
          ],
        ),
      ),
    );
    if (!mounted || action == null) return;
    await _doAction(entry, action);
  }

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
              key: const Key('file-rename-input'),
              controller: controller,
              autofocus: true,
              // 16px: web's mobile input rule — smaller text triggers
              // iOS auto-zoom on focus.
              style: const TextStyle(fontSize: 16),
              onSubmitted: (value) => Navigator.pop(context, value.trim()),
              decoration: const InputDecoration(labelText: '新名字'),
            ),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('取消')),
          FilledButton(
            key: const Key('file-rename-ok'),
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

  /// Delete one row or the whole checkbox selection. The dialog
  /// previews at most 5 names plus '等 N 项', like web's deleteFiles.
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
                  key: const Key('file-delete-recursive'),
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
              key: const Key('file-delete-ok'),
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

  Future<void> _preview(FileEntry entry) async {
    setState(() => _busy = true);
    List<int> bytes = const [];
    String? failure;
    try {
      bytes = await widget.api.readFile(widget.pane.ref, entry.path);
    } catch (error) {
      failure = '$error';
    }
    if (!mounted) return;
    setState(() => _busy = false);
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      backgroundColor: AppTheme.card,
      builder: (context) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: 0.8,
        maxChildSize: 0.95,
        builder: (context, controller) => Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 14, 8, 8),
              child: Row(children: [
                Expanded(
                  child: Text(entry.name,
                      maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w600)),
                ),
                TextButton(onPressed: () => _download(entry), child: const Text('下载')),
                IconButton(onPressed: () => Navigator.pop(context), icon: const Icon(Icons.close, size: 20)),
              ]),
            ),
            const Divider(height: 1),
            Expanded(
              child: failure != null
                  ? Center(child: Text(failure, style: TextStyle(color: AppTheme.danger)))
                  : _previewBody(entry, bytes, controller),
            ),
          ],
        ),
      ),
    );
  }

  Widget _previewBody(FileEntry entry, List<int> bytes, ScrollController controller) {
    final name = entry.name.toLowerCase();
    final isImage = RegExp(r'\.(png|jpe?g|gif|webp|svg)$').hasMatch(name);
    if (isImage && !name.endsWith('.svg')) {
      return InteractiveViewer(
        child: Center(child: Image.memory(Uint8List.fromList(bytes), fit: BoxFit.contain)),
      );
    }
    final text = utf8.decode(bytes, allowMalformed: true);
    final isMarkdown = RegExp(r'\.(md|markdown)$').hasMatch(name);
    if (isMarkdown) {
      return Markdown(
        data: text,
        controller: controller,
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
      );
    }
    return SingleChildScrollView(
      controller: controller,
      padding: const EdgeInsets.all(14),
      child: SelectableText(
        text.length > 400000 ? '${text.substring(0, 400000)}\n…' : text,
        style: AppTheme.mono,
      ),
    );
  }

  FileEntry? _entryForPath(String path) {
    final entries = _listing?.entries;
    if (entries == null) return null;
    for (final entry in entries) {
      if (entry.path == path) return entry;
    }
    return null;
  }

  /// F2 renames the single selection, else the focused row — web's
  /// file-list keydown rule (picked.length == 1 ? picked[0] : focused).
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

  @override
  Widget build(BuildContext context) {
    final listing = _listing;
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('文件 · ${widget.pane.label}', maxLines: 1, overflow: TextOverflow.ellipsis),
            Text(widget.pane.ref, style: TextStyle(fontSize: 11, fontWeight: FontWeight.w400, color: AppTheme.faint)),
          ],
        ),
        actions: [
          IconButton(
            key: const Key('files-zip'),
            tooltip: '打包当前目录',
            onPressed: _busy ? null : _zipCurrent,
            icon: const Icon(Icons.folder_zip_outlined, size: 20),
          ),
          IconButton(
            key: const Key('files-upload'),
            tooltip: '上传文件',
            onPressed: _busy ? null : _upload,
            icon: const Icon(Icons.upload_file, size: 20),
          ),
          IconButton(tooltip: '刷新', onPressed: _busy ? null : () => _open(_pathController.text), icon: const Icon(Icons.refresh, size: 20)),
        ],
      ),
      body: _dropWrap(CallbackShortcuts(
        bindings: {
          const SingleActivator(LogicalKeyboardKey.f2): _shortcutRename,
          const SingleActivator(LogicalKeyboardKey.delete): _shortcutDelete,
        },
        child: Focus(
          autofocus: true,
          child: SafeArea(
            top: false,
            child: Column(
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(12, 8, 12, 6),
                  child: Row(children: [
                    IconButton(
                      key: const Key('files-up'),
                      tooltip: '上级目录',
                      onPressed: listing?.parent == null ? null : () => _open(listing!.parent!),
                      icon: const Icon(Icons.arrow_upward, size: 18),
                    ),
                    Expanded(
                      child: TextField(
                        key: const Key('files-path'),
                        controller: _pathController,
                        focusNode: _pathFocusNode,
                        autocorrect: false,
                        style: AppTheme.mono,
                        textInputAction: TextInputAction.go,
                        onSubmitted: (value) => _open(value.trim()),
                        decoration: const InputDecoration(hintText: '绝对路径', isDense: true),
                      ),
                    ),
                  ]),
                ),
                // Web's file-foot status line, desktop only: the
                // panel itself is the drop surface.
                if (_isDesktop)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 2),
                    child: Text('拖拽文件到此处上传', style: TextStyle(fontSize: 11, color: AppTheme.muted)),
                  ),
                if (_busy) const LinearProgressIndicator(minHeight: 2),
                const Divider(height: 1),
                Expanded(child: _buildList()),
                if (_selected.isNotEmpty) _buildSelectionBar(),
              ],
            ),
          ),
        ),
      ),
    ),
    );
  }

  /// Footer for the checkbox selection, like web's .file-foot actions.
  /// Rename is only offered for exactly one pick, like web (its rename
  /// button is hidden unless picked.size == 1); batch rename is
  /// one-at-a-time, matching web's renameFile(path).
  Widget _buildSelectionBar() {
    final entries = _listing?.entries ?? const <FileEntry>[];
    final allSelected = entries.isNotEmpty && _selected.length == entries.length;
    final selectedEntries = _selected.map(_entryForPath).whereType<FileEntry>().toList();
    return Container(
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 10),
      decoration: BoxDecoration(border: Border(top: BorderSide(color: AppTheme.line))),
      // Horizontally scrollable instead of an Expanded count label:
      // the bar must survive narrow widths without forcing any
      // button to an infinite-width constraint.
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Text('已选 ${_selected.length} 项', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
          const SizedBox(width: 8),
        TextButton(
          key: const Key('files-select-all'),
          onPressed: () => setState(() {
            if (allSelected) {
              _selected.clear();
            } else {
              _selected.addAll(entries.map((entry) => entry.path));
            }
          }),
          child: Text(allSelected ? '取消全选' : '全选'),
        ),
        TextButton(
          key: const Key('files-select-none'),
          onPressed: () => setState(_selected.clear),
          child: const Text('清空'),
        ),
        if (_selected.length == 1)
          TextButton(
            key: const Key('files-footer-rename'),
            onPressed: selectedEntries.isEmpty ? null : () => _rename(selectedEntries.first),
            child: const Text('重命名'),
          ),
        TextButton(
          key: const Key('files-footer-delete'),
          onPressed: () => _deleteEntries(selectedEntries),
          child: Text('删除', style: TextStyle(color: AppTheme.danger)),
        ),
        // OutlinedButton, not FilledButton: the app theme gives
        // FilledButton an infinite minimum width, which only lays
        // out when the button fills its parent — in a Row with
        // siblings it asserts. Shares actions use OutlinedButton
        // for the same reason.
        OutlinedButton(
          key: const Key('files-footer-zip'),
          onPressed: _busy ? null : _zipSelection,
          child: const Text('下载 zip'),
        ),
        ]),
      ),
    );
  }

  Widget _buildList() {
    if (_loading && _listing == null) return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    if (_listing == null) {
      return ListView(padding: const EdgeInsets.all(24), children: [
        Text(_error ?? '加载失败', style: TextStyle(color: AppTheme.danger, fontSize: 13)),
        const SizedBox(height: 12),
        OutlinedButton(onPressed: () => _open(widget.pane.path), child: const Text('重试')),
      ]);
    }
    final entries = _listing!.entries;
    if (entries.isEmpty) {
      return Center(child: Text('这个目录是空的', style: TextStyle(color: AppTheme.muted)));
    }
    return RefreshIndicator(
      onRefresh: () => _open(_listing!.path),
      child: ListView.builder(
        itemCount: entries.length + (_listing!.truncated ? 1 : 0),
        itemBuilder: (context, index) {
          if (index == entries.length) {
            return Padding(
              padding: const EdgeInsets.all(16),
              child: Text('条目太多，只显示了一部分', style: TextStyle(fontSize: 12, color: AppTheme.muted)),
            );
          }
          final entry = entries[index];
          final selected = _selected.contains(entry.path);
          return Row(
            children: [
              Checkbox(
                key: Key('file-check-${entry.name}'),
                value: selected,
                onChanged: (value) => _toggleSelected(entry, value ?? false),
              ),
              Expanded(
                child: GestureDetector(
                  onSecondaryTapDown: (details) => _busy ? null : _showContextMenu(entry, details.globalPosition),
                  child: ListTile(
                    key: Key('file-${entry.name}'),
                    dense: true,
                    tileColor: selected ? AppTheme.accent.withValues(alpha: 0.08) : null,
                    leading: Icon(
                      entry.isDir ? Icons.folder_outlined : Icons.insert_drive_file_outlined,
                      size: 20,
                      color: entry.isDir ? AppTheme.accent : AppTheme.faint,
                    ),
                    title: Text(entry.name,
                        maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 14)),
                    subtitle: entry.isDir ? null : Text(entry.sizeLabel, style: TextStyle(fontSize: 11, color: AppTheme.faint)),
                    trailing: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        if (!entry.isDir)
                          IconButton(
                            tooltip: '下载',
                            onPressed: _busy ? null : () => _download(entry),
                            icon: const Icon(Icons.download_outlined, size: 18),
                          ),
                        IconButton(
                          key: Key('file-more-${entry.name}'),
                          tooltip: '更多',
                          onPressed: _busy ? null : () => _actions(entry),
                          icon: const Icon(Icons.more_vert, size: 18),
                        ),
                      ],
                    ),
                    onTap: () {
                      _focusedPath = entry.path;
                      entry.isDir ? _open(entry.path) : _preview(entry);
                    },
                    onLongPress: _busy ? null : () {
                      _focusedPath = entry.path;
                      _actions(entry);
                    },
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

/// Dashed rounded-rectangle outline, matching web's
/// `.file-panel.dragging { outline: 2px dashed accent }`.
class _DashedBorderPainter extends CustomPainter {
  const _DashedBorderPainter(this.color);

  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    const inset = 6.0;
    final rect = Rect.fromLTWH(inset, inset, size.width - inset * 2, size.height - inset * 2);
    final path = Path()..addRRect(RRect.fromRectAndRadius(rect, const Radius.circular(10)));
    final paint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2;
    const dash = 8.0;
    const gap = 6.0;
    for (final metric in path.computeMetrics()) {
      var distance = 0.0;
      while (distance < metric.length) {
        final end = (distance + dash).clamp(0.0, metric.length);
        canvas.drawPath(metric.extractPath(distance, end), paint);
        distance += dash + gap;
      }
    }
  }

  @override
  bool shouldRepaint(_DashedBorderPainter oldDelegate) => oldDelegate.color != color;
}
