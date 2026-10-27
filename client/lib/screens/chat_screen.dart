import 'dart:async';

import 'package:flutter/material.dart';

import '../models/chat_source.dart';
import '../models/models.dart';
import '../theme.dart';
import '../widgets/message_tile.dart';

/// Chat view for a pane or a share link: polls the transcript, renders messages
/// with per-message and per-block copy controls, sends input when allowed.
class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key, required this.source, this.poll = const Duration(seconds: 2)});

  final ChatSource source;
  final Duration poll;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final _controller = TextEditingController();
  final _scroll = ScrollController();
  final List<String> _pending = [];

  Transcript? _data;
  String? _error;
  bool _loading = true;
  bool _sending = false;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _refresh(initial: true);
    if (widget.poll > Duration.zero) {
      _timer = Timer.periodic(widget.poll, (_) => _refresh());
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    _controller.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _refresh({bool initial = false}) async {
    try {
      final transcript = await widget.source.fetch();
      if (!mounted) return;
      final previous = _data;
      final changed = previous == null ||
          previous.mtime != transcript.mtime ||
          previous.content.length != transcript.content.length ||
          previous.messages.length != transcript.messages.length;
      if (!changed && !initial) return;
      final atBottom = !_scroll.hasClients || _scroll.position.pixels >= _scroll.position.maxScrollExtent - 80;
      final seen = transcript.messages.where((message) => message.isUser).map((message) => message.text.trim()).toSet();
      setState(() {
        _data = transcript;
        _error = null;
        _loading = false;
        _pending.removeWhere((text) => seen.contains(text.trim()));
      });
      if (atBottom) _scrollToEnd();
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _error = '$error';
        _loading = false;
      });
    }
  }

  void _scrollToEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      _scroll.animateTo(_scroll.position.maxScrollExtent,
          duration: const Duration(milliseconds: 180), curve: Curves.easeOut);
    });
  }

  Future<void> _send() async {
    final text = _controller.text;
    if (text.trim().isEmpty || _sending) return;
    setState(() {
      _sending = true;
      _pending.add(text);
      _controller.clear();
    });
    _scrollToEnd();
    try {
      await widget.source.send(text);
      await _refresh();
    } catch (error) {
      if (!mounted) return;
      setState(() => _pending.removeLast());
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final data = _data;
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(widget.source.title, maxLines: 1, overflow: TextOverflow.ellipsis),
            if (widget.source.subtitle.isNotEmpty)
              Text(
                widget.source.subtitle,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w400, color: AppTheme.faint),
              ),
          ],
        ),
        actions: [
          if (widget.source.readOnly)
            const Padding(padding: EdgeInsets.only(right: 8), child: Center(child: _Badge(label: '只读', accent: true)))
          else if (data?.provider != null)
            Padding(padding: const EdgeInsets.only(right: 8), child: Center(child: _Badge(label: data!.provider!))),
          IconButton(
            tooltip: '刷新',
            onPressed: () => _refresh(initial: true),
            icon: const Icon(Icons.refresh, size: 20),
          ),
        ],
      ),
      body: Column(children: [Expanded(child: _buildBody(data)), _buildComposer()]),
    );
  }

  Widget _buildBody(Transcript? data) {
    if (_loading && data == null) return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    if (data == null) {
      return _ErrorState(message: _error ?? '没有可显示的内容', onRetry: () => _refresh(initial: true));
    }
    if (data.isScreen) {
      return SingleChildScrollView(
        padding: const EdgeInsets.all(14),
        child: Container(
          width: double.infinity,
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(color: const Color(0xFF272622), borderRadius: BorderRadius.circular(10)),
          child: SelectableText(
            data.content,
            style: const TextStyle(fontFamily: 'monospace', fontSize: 11.5, height: 1.45, color: Color(0xFFF5F4ED)),
          ),
        ),
      );
    }
    final messages = data.messages;
    final offset = _error != null ? 1 : 0;
    return ListView.builder(
      key: const Key('chat-list'),
      controller: _scroll,
      padding: const EdgeInsets.fromLTRB(14, 10, 14, 16),
      itemCount: messages.length + _pending.length + offset,
      itemBuilder: (context, index) {
        if (_error != null && index == 0) {
          return Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text(_error!, style: const TextStyle(color: Color(0xFFB34B43), fontSize: 12)),
          );
        }
        final position = index - offset;
        if (position < messages.length) return MessageTile(message: messages[position]);
        final text = _pending[position - messages.length];
        return MessageTile(message: ChatMessage(id: 'pending-$index', role: 'user', text: text));
      },
    );
  }

  Widget _buildComposer() {
    if (widget.source.readOnly) {
      return Container(
        key: const Key('read-only-banner'),
        width: double.infinity,
        padding: EdgeInsets.fromLTRB(16, 12, 16, 12 + MediaQuery.of(context).padding.bottom),
        decoration: const BoxDecoration(color: AppTheme.page, border: Border(top: BorderSide(color: AppTheme.line))),
        child: const Text(
          '只读分享：可以查看内容，但不能发送消息。',
          textAlign: TextAlign.center,
          style: TextStyle(fontSize: 12.5, color: AppTheme.muted),
        ),
      );
    }
    return Container(
      padding: EdgeInsets.fromLTRB(12, 8, 12, 8 + MediaQuery.of(context).padding.bottom),
      decoration: const BoxDecoration(color: AppTheme.page, border: Border(top: BorderSide(color: AppTheme.line))),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Expanded(
            child: TextField(
              key: const Key('chat-composer'),
              controller: _controller,
              minLines: 1,
              maxLines: 5,
              textInputAction: TextInputAction.newline,
              style: const TextStyle(fontSize: 14.5, height: 1.4),
              decoration: const InputDecoration(hintText: '继续提问…'),
            ),
          ),
          const SizedBox(width: 8),
          SizedBox(
            height: 46,
            width: 46,
            child: FilledButton(
              key: const Key('chat-send'),
              onPressed: _sending ? null : _send,
              style: FilledButton.styleFrom(padding: EdgeInsets.zero, minimumSize: const Size(46, 46), shape: const CircleBorder()),
              child: _sending
                  ? const SizedBox(height: 16, width: 16, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                  : const Icon(Icons.arrow_upward, size: 20),
            ),
          ),
        ],
      ),
    );
  }
}

class _Badge extends StatelessWidget {
  const _Badge({required this.label, this.accent = false});

  final String label;
  final bool accent;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: accent ? const Color(0xFFF6E7E0) : const Color(0xFFE8E6DE),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(label, style: TextStyle(fontSize: 11, color: accent ? AppTheme.accent : AppTheme.muted)),
    );
  }
}

class _ErrorState extends StatelessWidget {
  const _ErrorState({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(message, textAlign: TextAlign.center, style: const TextStyle(color: AppTheme.muted, fontSize: 13)),
            const SizedBox(height: 12),
            OutlinedButton(onPressed: onRetry, child: const Text('重试')),
          ],
        ),
      ),
    );
  }
}
