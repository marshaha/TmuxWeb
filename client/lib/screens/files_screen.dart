import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:path_provider/path_provider.dart';

import '../api/api_client.dart';
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
    final directory = await getApplicationDocumentsDirectory();
    final folder = Directory('${directory.path}/tmuxweb');
    await folder.create(recursive: true);
    final file = File('${folder.path}/$name');
    await file.writeAsBytes(bytes);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('已保存到 ${file.path}')));
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
                  ? Center(child: Text(failure, style: const TextStyle(color: AppTheme.danger)))
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
            Text(widget.pane.ref, style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w400, color: AppTheme.faint)),
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
        Text(_error ?? '加载失败', style: const TextStyle(color: AppTheme.danger, fontSize: 13)),
        const SizedBox(height: 12),
        OutlinedButton(onPressed: () => _open(widget.pane.path), child: const Text('重试')),
      ]);
    }
    final entries = _listing!.entries;
    if (entries.isEmpty) {
      return const Center(child: Text('这个目录是空的', style: TextStyle(color: AppTheme.muted)));
    }
    return RefreshIndicator(
      onRefresh: () => _open(_listing!.path),
      child: ListView.builder(
        itemCount: entries.length + (_listing!.truncated ? 1 : 0),
        itemBuilder: (context, index) {
          if (index == entries.length) {
            return const Padding(
              padding: EdgeInsets.all(16),
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
            subtitle: entry.isDir ? null : Text(entry.sizeLabel, style: const TextStyle(fontSize: 11, color: AppTheme.faint)),
            trailing: entry.isDir
                ? const Icon(Icons.chevron_right, size: 18, color: AppTheme.faint)
                : IconButton(
                    tooltip: '下载',
                    onPressed: _busy ? null : () => _download(entry),
                    icon: const Icon(Icons.download_outlined, size: 18),
                  ),
            onTap: () => entry.isDir ? _open(entry.path) : _preview(entry),
          );
        },
      ),
    );
  }
}
