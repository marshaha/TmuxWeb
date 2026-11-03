import 'dart:convert';
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import '../api/api_client.dart';
import '../services/save_file.dart';
import '../models/models.dart';
import '../theme.dart';

/// File panel for one pane: browse, preview, download, upload and zip.
class FilesScreen extends StatefulWidget {
  const FilesScreen({super.key, required this.api, required this.pane});

  final Api api;
  final Pane pane;

  @override
  State<FilesScreen> createState() => _FilesScreenState();
}

class _FilesScreenState extends State<FilesScreen> {
  final _pathController = TextEditingController();
  FileListing? _listing;
  String? _error;
  bool _loading = true;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _open(widget.pane.path);
  }

  @override
  void dispose() {
    _pathController.dispose();
    super.dispose();
  }

  Future<void> _open(String path) async {
    setState(() => _loading = true);
    try {
      final listing = await widget.api.listFiles(widget.pane.ref, path: path);
      if (!mounted) return;
      setState(() {
        _listing = listing;
        _pathController.text = listing.path;
        _error = null;
        _loading = false;
      });
    } catch (error) {
      if (!mounted) return;
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

  Future<void> _upload() async {
    final listing = _listing;
    if (listing == null) return;
    final picked = await FilePicker.pickFiles();
    if (picked.isEmpty) return;
    final file = picked.first;
    final bytes = await file.xFile.readAsBytes();
    setState(() => _busy = true);
    try {
      await widget.api.uploadFile(widget.pane.ref, listing.path, file.name, bytes);
      await _open(listing.path);
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('已上传 ${file.name}')));
    } catch (error) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
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
            if (!entry.isDir)
              ListTile(
                leading: const Icon(Icons.download_outlined, size: 20),
                title: const Text('下载'),
                onTap: () => Navigator.pop(context, 'download'),
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
    switch (action) {
      case 'open':
        entry.isDir ? await _open(entry.path) : await _preview(entry);
      case 'download':
        await _download(entry);
      case 'rename':
        await _rename(entry);
      case 'delete':
        await _delete(entry);
    }
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

  Future<void> _delete(FileEntry entry) async {
    var recursive = entry.isDir;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: const Text('删除'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(entry.name, style: const TextStyle(fontWeight: FontWeight.w600)),
              const SizedBox(height: 4),
              Text('删除后无法恢复。', style: TextStyle(fontSize: 12, color: AppTheme.danger)),
              if (entry.isDir)
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
      await widget.api.deleteFiles(widget.pane.ref, [entry.path], recursive: recursive);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('已删除 ${entry.name}')));
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
      body: Column(
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
                  autocorrect: false,
                  style: AppTheme.mono,
                  textInputAction: TextInputAction.go,
                  onSubmitted: (value) => _open(value.trim()),
                  decoration: const InputDecoration(hintText: '绝对路径', isDense: true),
                ),
              ),
            ]),
          ),
          if (_busy) const LinearProgressIndicator(minHeight: 2),
          const Divider(height: 1),
          Expanded(child: _buildList()),
        ],
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
          return ListTile(
            key: Key('file-${entry.name}'),
            dense: true,
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
            onTap: () => entry.isDir ? _open(entry.path) : _preview(entry),
            onLongPress: _busy ? null : () => _actions(entry),
          );
        },
      ),
    );
  }
}
