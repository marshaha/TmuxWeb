import 'dart:async';
import 'dart:convert';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/api_client.dart';
import '../models/chat_source.dart';
import '../services/save_file.dart';
import '../services/screen_stream.dart';
import 'files_screen.dart';
import 'hosts_screen.dart';
import 'terminal_screen.dart';
import '../models/models.dart';
import '../models/transcript_cache.dart';
import '../theme.dart';
import '../widgets/attachment_bar.dart';
import '../widgets/message_tile.dart';
import '../widgets/path_link_text.dart';

/// Chat view for a pane or a share link: transcript mode polls, screen
/// mode streams live captures over the pane socket, and the live
/// toggle pauses either one while keeping the content on screen
/// with per-message and per-block copy controls, sends input when allowed.
///
/// With [embedded] true, the screen renders only the transcript + composer
/// column, no Scaffold and no AppBar, so a caller can put it inside its own
/// panel or page (the board's right-side preview drawer, the full preview
/// page). The state, polling, cache and composer are identical in both
/// modes; only the chrome differs.
class ChatScreen extends StatefulWidget {
  const ChatScreen({
    super.key,
    required this.source,
    this.poll = const Duration(seconds: 1),
    this.cache,
    this.terminalApi,
    this.embedded = false,
    this.baseUrl = '',
    this.pickAttachments,
  });

  final ChatSource source;
  final Duration poll;

  /// Server origin, for building full share links. Empty when the
  /// caller does not know it; the share dialog then shows the path.
  final String baseUrl;

  /// Enables the terminal entry when the caller also has a socket-capable API.
  final TerminalApi? terminalApi;

  /// Injectable for tests; defaults to the process-wide cache.
  final TranscriptCache? cache;

  final bool embedded;

  /// Injectable file picker for tests; defaults to FilePicker. The
  /// returned items are uploaded by ChatScreen on pick (names are
  /// deduplicated there, like web's uniqueName).
  final Future<List<PendingAttachment>> Function()? pickAttachments;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final _controller = TextEditingController();
  final _scroll = ScrollController();
  final List<String> _pending = [];

  /// 发送失败的消息（按文本记），气泡旁边给一个「重发」，跟 web 一样不把内容吞掉。
  final Set<String> _failed = {};

  /// 输入框下面那一行状态：'' 中性 / '已发送' 绿 / '发送失败' 红。
  String _sendState = '';
  bool _sendStateOk = false;
  Timer? _sendStateTimer;

  Transcript? _data;
  String? _error;
  bool _loading = true;
  bool _sending = false;
  bool _stopping = false;
  bool _stale = false;
  bool _paused = false;
  bool _fullscreen = false;
  bool _reconnecting = false;
  Timer? _timer;
  StreamSubscription<String>? _screenSub;
  StreamSubscription<bool>? _reconnectSub;
  bool _screenClosedByUs = false;

  /// Pending composer attachments (pane sources only). Cleared when
  /// the pane changes and after a successful send, like web's
  /// clearAttachments on pane switch / send success.
  final List<PendingAttachment> _attachments = [];

  TranscriptCache get _cache => widget.cache ?? TranscriptCache.instance;
  String get _key => widget.source.cacheKey;

  @override
  void initState() {
    super.initState();
    final cached = _cache.read(_key);
    if (cached != null) {
      // paint what we already had, then revalidate in the background
      _data = cached.transcript;
      _loading = false;
      _stale = true;
      _pending.clear();
    }
    _controller.text = _cache.draft(_key);
    _controller.addListener(() => _cache.saveDraft(_key, _controller.text));
    _scroll.addListener(_rememberScroll);
    _refresh(initial: true);
    _startPolling();
    if (cached != null) {
      WidgetsBinding.instance
          .addPostFrameCallback((_) => _restoreScroll(cached));
    }
  }

  @override
  void didUpdateWidget(ChatScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.source.cacheKey != widget.source.cacheKey) {
      // Web clears attachments on pane switch (selectPane); the old
      // screen stream must not keep feeding another pane's frames.
      _attachments.clear();
      _stopScreenStream();
      _reconnecting = false;
      _startPolling();
    }
  }

  @override
  void dispose() {
    _rememberScroll();
    _timer?.cancel();
    _sendStateTimer?.cancel();
    _stopScreenStream();
    unawaited(_reconnectSub?.cancel());
    _controller.removeListener(_rememberScroll);
    _controller.dispose();
    _scroll.removeListener(_rememberScroll);
    _scroll.dispose();
    super.dispose();
  }

  /// Web's canAutoFocusComposer (index.html:365): only on non-mobile,
  /// non-touch targets. Flutter has no pointer-kind MediaQuery, so the
  /// target platform stands in: iOS / Android / Fuchsia never
  /// autofocus, desktop and web-desktop do.
  bool get _canAutoFocus {
    switch (defaultTargetPlatform) {
      case TargetPlatform.iOS:
      case TargetPlatform.android:
      case TargetPlatform.fuchsia:
        return false;
      case TargetPlatform.linux:
      case TargetPlatform.macOS:
      case TargetPlatform.windows:
        return true;
    }
  }

  bool get _isTouchDevice {
    switch (defaultTargetPlatform) {
      case TargetPlatform.iOS:
      case TargetPlatform.android:
      case TargetPlatform.fuchsia:
        return true;
      case TargetPlatform.linux:
      case TargetPlatform.macOS:
      case TargetPlatform.windows:
        return false;
    }
  }

  /// Display title with web's `#N` short-number prefix
  /// (index.html:301,1001). Null/empty number → plain title, never an
  /// invented number. Display only; addressing is untouched.
  String get _displayTitle {
    final number = widget.source.numberLabel;
    if (number == null) return widget.source.title;
    return '$number · ${widget.source.title}';
  }

  void _startPolling() {
    _timer?.cancel();
    if (!_paused && widget.poll > Duration.zero) {
      _timer = Timer.periodic(widget.poll, (_) => _refresh());
    }
  }

  /// Pauses live refresh, like web's live-toggle: polling and the
  /// screen socket stop, but the content stays on screen. Resuming
  /// re-attaches — for a screen pane the first capture frame
  /// repaints the frozen screen, so there is no blank gap.
  void _toggleLive() {
    if (_paused) {
      setState(() => _paused = false);
      _startPolling();
      _refresh();
    } else {
      setState(() => _paused = true);
      _timer?.cancel();
      _stopScreenStream();
    }
  }

  void _startScreenStream() {
    if (_screenSub != null || _paused) return;
    _timer?.cancel();
    _screenClosedByUs = false;
    // screenStream() first: for pane sources it creates the ScreenStream
    // whose reconnecting state the subscription below must observe.
    _screenSub = widget.source.screenStream().listen(
      (frame) {
        if (!mounted) return;
        final data = _data;
        if (data == null || !data.isScreen) return;
        setState(() {
          _data = Transcript(
            mode: data.mode,
            provider: data.provider,
            sessionId: data.sessionId,
            content: trimCapture(stripAnsi(frame)),
            mtime: data.mtime,
            readOnly: data.readOnly,
            waiting: data.waiting,
            waitingSince: data.waitingSince,
          );
        });
      },
      onDone: _screenStreamUnavailable,
      onError: (_) => _screenStreamUnavailable(),
    );
    unawaited(_reconnectSub?.cancel());
    _reconnectSub = widget.source.screenReconnecting().listen((value) {
      if (mounted) setState(() => _reconnecting = value);
    });
    _reconnecting = widget.source.isScreenReconnecting;
  }

  /// The source has no live stream (shares, or an API without socket
  /// support): fall back to polling instead of going stale.
  void _screenStreamUnavailable() {
    _screenSub = null;
    unawaited(_reconnectSub?.cancel());
    _reconnectSub = null;
    if (mounted) setState(() => _reconnecting = false);
    if (!_screenClosedByUs && mounted && !_paused) _startPolling();
  }

  void _stopScreenStream() {
    _screenClosedByUs = true;
    final sub = _screenSub;
    _screenSub = null;
    unawaited(sub?.cancel());
    unawaited(_reconnectSub?.cancel());
    _reconnectSub = null;
    _reconnecting = false;
  }

  void _rememberScroll() {
    if (!_scroll.hasClients || _data == null) return;
    _cache.saveScroll(_key, _scroll.position.pixels,
        stickToBottom: _nearBottom);
  }

  bool get _nearBottom =>
      !_scroll.hasClients ||
      _scroll.position.pixels >= _scroll.position.maxScrollExtent - 80;

  void _restoreScroll(CachedConversation cached) {
    if (!_scroll.hasClients) return;
    final target = cached.scrollOffset;
    if (target == null || cached.stickToBottom) {
      _scroll.jumpTo(_scroll.position.maxScrollExtent);
    } else {
      _scroll.jumpTo(target.clamp(0.0, _scroll.position.maxScrollExtent));
    }
  }

  Future<void> _refresh({bool initial = false, bool force = false}) async {
    try {
      final transcript = await widget.source.fetch(refresh: force);
      if (!mounted) return;
      _cache.write(_key, transcript);
      final previous = _data;
      final changed = previous == null ||
          previous.mtime != transcript.mtime ||
          previous.content.length != transcript.content.length ||
          previous.messages.length != transcript.messages.length;
      if (!changed && !initial) return;
      final atBottom = !_scroll.hasClients ||
          _scroll.position.pixels >= _scroll.position.maxScrollExtent - 80;
      final seen = transcript.messages
          .where((message) => message.isUser)
          .map((message) => message.text.trim())
          .toSet();
      setState(() {
        _data = transcript;
        _error = null;
        _loading = false;
        _stale = false;
        _pending.removeWhere((text) => seen.contains(text.trim()));
      });
      if (transcript.isScreen) {
        _startScreenStream();
      } else if (_screenSub != null) {
        _stopScreenStream();
        _startPolling();
      }
      if (atBottom || (initial && previous == null)) _scrollToEnd();
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

  /// True while the agent still owes an answer for the last user message.
  bool get _waiting => _data?.waiting == true;

  /// The composer offers a stop button while the agent works and nothing is typed.
  bool get _showStop =>
      _waiting && _controller.text.trim().isEmpty && !widget.source.readOnly;

  Future<void> _stop() async {
    if (_stopping) return;
    setState(() => _stopping = true);
    try {
      final key = await widget.source.stop();
      if (!mounted) return;
      final label = key == 'C-q' ? 'Ctrl+Q' : 'Esc';
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('已发送 $label，正在停止')));
      await _refresh();
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('$error')));
    } finally {
      if (mounted) setState(() => _stopping = false);
    }
  }

  Future<void> _send() async {
    // Web refuses to send while an attachment PUT is still in flight.
    if (_attachments.any((item) => !item.uploaded)) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('附件上传中，请稍候再发送')));
      return;
    }
    // Web's sendInput: the attachment header line goes first, then the
    // typed text; a prompt-only send (attachments, no text) is allowed.
    final typed = _controller.text;
    final prompt = attachmentPrompt(_attachments);
    final text = prompt.isEmpty
        ? typed
        : (typed.trim().isEmpty ? prompt : '$prompt\n\n$typed');
    if (text.trim().isEmpty || _sending) return;
    setState(() {
      _sending = true;
      _pending.add(text);
      _controller.clear();
    });
    _scrollToEnd();
    try {
      await widget.source.send(text);
      // Web clears attachments right after a successful POST, before
      // the refresh; a failed send keeps them for the retry.
      if (mounted) setState(() => _attachments.clear());
    } catch (error) {
      if (!mounted) return;
      // 发送这一步失败 → 那条气泡留在原位并标成失败，可以重发。
      setState(() {
        _failed.add(text);
        _setSendState('发送失败', ok: false);
      });
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('$error')));
      if (mounted) setState(() => _sending = false);
      return;
    }
    try {
      await _refresh();
      if (!mounted) return;
      setState(() {
        _failed.remove(text);
        _setSendState('已发送', ok: true);
      });
    } catch (error) {
      // 发成功了、只是刷新失败：不能说发送失败，更不能把消息撤回（会诱导用户重发成两条）。
      if (!mounted) return;
      setState(() => _setSendState('已发送', ok: true));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  void _setSendState(String text, {required bool ok}) {
    _sendState = text;
    _sendStateOk = ok;
    _sendStateTimer?.cancel();
    if (ok) {
      // 成功状态 1.4 秒后自己消失，失败状态留着直到下一次操作（web 的 setSendState）。
      _sendStateTimer = Timer(const Duration(milliseconds: 1400), () {
        if (mounted) setState(() => _sendState = '');
      });
    }
  }

  /// 重发一条失败的消息，成功后就地从失败集合里摘掉。
  Future<void> _resend(String text) async {
    if (_sending) return;
    setState(() {
      _sending = true;
      _setSendState('重发中…', ok: false);
    });
    try {
      await widget.source.send(text);
      await _refresh();
      if (!mounted) return;
      setState(() {
        _failed.remove(text);
        _setSendState('已发送', ok: true);
      });
    } catch (error) {
      if (!mounted) return;
      setState(() => _setSendState('发送失败', ok: false));
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('$error')));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  // --- Chat attachments (web composer-files.js, shared console + grid
  // via this one ChatScreen) -------------------------------------------

  /// `<pane cwd>/upload_files`, or '' when the pane has no directory —
  /// web then refuses the attach with the same message.
  String _uploadDir(PaneChatSource source) {
    final base = source.pane.path.replaceAll(RegExp(r'/+$'), '');
    return base.isEmpty ? '' : '$base/upload_files';
  }

  Future<List<PendingAttachment>> _defaultPickAttachments() async {
    // file_picker 13 returns the picked files (empty on cancel); the
    // same xFile.readAsBytes pattern files_screen already uses.
    final picked = await FilePicker.pickFiles();
    final items = <PendingAttachment>[];
    for (final file in picked) {
      final bytes = await file.xFile.readAsBytes();
      items.add(PendingAttachment(
          name: file.name, bytes: bytes, isImage: isImageFileName(file.name)));
    }
    return items;
  }

  Future<void> _pickAttachments() async {
    final source = widget.source;
    if (source is! PaneChatSource) return;
    final dir = _uploadDir(source);
    if (dir.isEmpty) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('这个会话没有可用目录，无法上传附件')));
      return;
    }
    final picked =
        await (widget.pickAttachments?.call() ?? _defaultPickAttachments());
    for (final file in picked) {
      if (file.bytes.length > 256 * 1024 * 1024) {
        if (mounted) {
          ScaffoldMessenger.of(context)
              .showSnackBar(SnackBar(content: Text('${file.name} 超过 256 MB')));
        }
        continue;
      }
      final name = uniqueAttachmentName(
        file.name,
        _attachments.map((item) => item.name).toSet(),
      );
      final item = PendingAttachment(
          name: name, bytes: file.bytes, isImage: file.isImage);
      setState(() => _attachments.add(item));
      try {
        await source.api.uploadFile(source.pane.ref, dir, name, file.bytes);
        if (mounted) setState(() => item.uploaded = true);
      } catch (error) {
        if (mounted) {
          setState(() => _attachments.remove(item));
          ScaffoldMessenger.of(context)
              .showSnackBar(SnackBar(content: Text('附件上传失败：$error')));
        }
      }
    }
  }

  void _removeAttachment(int index) {
    if (index < 0 || index >= _attachments.length) return;
    final item = _attachments[index];
    final source = widget.source;
    setState(() => _attachments.removeAt(index));
    // Web fires the DELETE best-effort and ignores failures.
    if (item.uploaded && source is PaneChatSource) {
      final dir = _uploadDir(source);
      if (dir.isNotEmpty) {
        unawaited(source.api
            .deleteUploadFile(source.pane.ref, dir, item.name)
            .catchError((_) => <String, dynamic>{}));
      }
    }
  }

  // --- Path links (web path-links.js) ---------------------------------

  PathLinkActions? get _pathLinkActions {
    final api = widget.source.chatApi;
    final ref = widget.source.paneRef;
    if (api == null || ref == null) return null;
    return PathLinkActions(
      resolve: (raw) => api.fileStat(ref, raw),
      onPreview: _previewPath,
      onDownload: _downloadPath,
      onOpenDir: _openPathDir,
    );
  }

  Future<void> _previewPath(String path, Map<String, dynamic> stat) async {
    final api = widget.source.chatApi;
    final ref = widget.source.paneRef;
    if (api == null || ref == null) return;
    try {
      final bytes = await api.readFile(ref, path);
      var text = utf8.decode(bytes, allowMalformed: true);
      if (text.length > 160000) {
        text = '${text.substring(0, 160000)}\n…（已截断，只显示前 160000 字）';
      }
      if (!mounted) return;
      final name =
          '${stat['name'] ?? path.split('/').where((part) => part.isNotEmpty).lastOrNull ?? path}';
      await showDialog<void>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          title: Text(name),
          content: SingleChildScrollView(child: SelectableText(text)),
          actions: [
            TextButton(
                onPressed: () => Navigator.of(dialogContext).pop(),
                child: const Text('关闭')),
          ],
        ),
      );
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$error')));
      }
    }
  }

  Future<void> _downloadPath(String path, Map<String, dynamic> stat) async {
    final api = widget.source.chatApi;
    final ref = widget.source.paneRef;
    if (api == null || ref == null) return;
    final name =
        '${stat['name'] ?? path.split('/').where((part) => part.isNotEmpty).lastOrNull ?? path}';
    // Web downloads.js maybe(): touch devices confirm before the
    // download (iOS Safari can strand the user in a preview page).
    if (_isTouchDevice) {
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          title: const Text('下载文件'),
          content: Text('$name\n${humanSize((stat['size'] as num?) ?? 0)}'),
          actions: [
            TextButton(
                onPressed: () => Navigator.of(dialogContext).pop(false),
                child: const Text('取消')),
            FilledButton(
              key: const Key('path-download-confirm'),
              onPressed: () => Navigator.of(dialogContext).pop(true),
              child: const Text('下载'),
            ),
          ],
        ),
      );
      if (confirmed != true) return;
    }
    try {
      final bytes = await api.readFile(ref, path);
      final saved = await saveFileBytes(name, bytes);
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('已保存到 $saved')));
      }
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$error')));
      }
    }
  }

  void _openPathDir(String path) {
    final source = widget.source;
    if (source is! PaneChatSource) return;
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) =>
          FilesScreen(api: source.api, pane: source.pane, initialPath: path),
    ));
  }

  /// Session cookie for mermaid rendering, derived from the pane
  /// source's API when it carries one (TerminalApi); empty for share
  /// sources, which render mermaid source-only.
  String get _cookieHeader {
    final api = widget.source.chatApi;
    // Explicit `as` cast (the codebase's established pattern): property
    // and getter accesses do not get `is`-promotion here.
    if (api is TerminalApi) return (api as TerminalApi).wsHeaders['Cookie'] ?? '';
    return '';
  }

  @override
  Widget build(BuildContext context) {
    final data = _data;
    if (widget.embedded) {
      return Column(children: [
        if (_reconnecting)
          Container(
            width: double.infinity,
            padding: const EdgeInsets.symmetric(vertical: 6),
            color: const Color(0xFFE8E6DE),
            child: Text('重连中…',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 11.5, color: AppTheme.muted)),
          ),
        Expanded(child: _buildBody(data)),
        _buildComposer(),
      ]);
    }
    if (_fullscreen) {
      // Fullscreen conversation mode (web index.html:74-84): the
      // conversation fills the screen and the AppBar chrome is hidden;
      // the floating button exits. Scope: standalone ChatScreen only —
      // embedded hosts (grid cells, preview drawer) own their chrome,
      // including the grid cell maximize, which is a different feature.
      return Scaffold(
        body: Stack(
          children: [
            Column(children: [
              Expanded(child: _buildBody(data)),
              _buildComposer()
            ]),
            Positioned(
              top: 8,
              right: 8,
              child: Container(
                decoration: BoxDecoration(
                    color: AppTheme.card,
                    borderRadius: BorderRadius.circular(999)),
                child: IconButton(
                  key: const Key('chat-fullscreen-exit'),
                  tooltip: '退出全屏',
                  onPressed: () => setState(() => _fullscreen = false),
                  icon: const Icon(Icons.fullscreen_exit, size: 20),
                ),
              ),
            ),
          ],
        ),
      );
    }
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(_displayTitle, maxLines: 1, overflow: TextOverflow.ellipsis),
            if (widget.source.subtitle.isNotEmpty)
              Text(
                widget.source.subtitle,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                    fontSize: 11,
                    fontWeight: FontWeight.w400,
                    color: AppTheme.faint),
              ),
          ],
        ),
        actions: [
          if (widget.terminalApi != null && widget.source is PaneChatSource)
            IconButton(
              key: const Key('chat-files'),
              tooltip: '文件',
              onPressed: () {
                final source = widget.source;
                if (source is! PaneChatSource) return;
                Navigator.of(context).push(MaterialPageRoute(
                  builder: (_) =>
                      FilesScreen(api: source.api, pane: source.pane),
                ));
              },
              icon: const Icon(Icons.folder_outlined, size: 20),
            ),
          if (widget.source is PaneChatSource)
            IconButton(
              key: const Key('chat-share'),
              tooltip: '分享',
              onPressed: () {
                final source = widget.source;
                if (source is! PaneChatSource) return;
                showDialog(
                  context: context,
                  builder: (_) => _CreateShareDialog(
                      api: source.api,
                      pane: source.pane,
                      baseUrl: widget.baseUrl),
                );
              },
              icon: const Icon(Icons.share_outlined, size: 20),
            ),
          if (widget.terminalApi != null)
            IconButton(
              key: const Key('chat-terminal'),
              tooltip: '终端',
              onPressed: () {
                final api = widget.terminalApi;
                final source = widget.source;
                if (api == null || source is! PaneChatSource) return;
                Navigator.of(context).push(MaterialPageRoute(
                  builder: (_) => TerminalScreen(
                    api: api,
                    pane: source.pane,
                    onOpenInConsole: () => Navigator.of(context).push(
                        MaterialPageRoute(
                            builder: (_) => HostsScreen(
                                api: source.api,
                                baseUrl: widget.baseUrl,
                                initialPaneRef: source.pane.ref))),
                  ),
                ));
              },
              icon: const Icon(Icons.terminal, size: 20),
            ),
          if (_paused)
            const Padding(
                padding: EdgeInsets.only(right: 4),
                child: Center(child: _Badge(label: '已暂停'))),
          if (_reconnecting)
            const Padding(
                padding: EdgeInsets.only(right: 4),
                child: Center(child: _Badge(label: '重连中…'))),
          if (_stale)
            const Padding(
                padding: EdgeInsets.only(right: 4),
                child: Center(child: _Badge(label: '更新中…'))),
          IconButton(
            key: const Key('chat-fullscreen'),
            tooltip: _fullscreen ? '退出全屏' : '对话全屏',
            onPressed: () => setState(() => _fullscreen = !_fullscreen),
            icon: Icon(_fullscreen ? Icons.fullscreen_exit : Icons.fullscreen,
                size: 20),
          ),
          IconButton(
            key: const Key('chat-live-toggle'),
            tooltip: _paused ? '继续实时刷新' : '暂停实时刷新',
            onPressed: _toggleLive,
            icon: Icon(_paused ? Icons.play_arrow : Icons.pause, size: 20),
          ),
          if (widget.source.readOnly)
            const Padding(
                padding: EdgeInsets.only(right: 8),
                child: Center(child: _Badge(label: '只读', accent: true)))
          else if (data?.provider != null)
            Padding(
                padding: const EdgeInsets.only(right: 8),
                child: Center(child: _Badge(label: data!.provider!))),
          IconButton(
            tooltip: '重新读取（忽略缓存）',
            onPressed: () => _refresh(initial: true, force: true),
            icon: const Icon(Icons.refresh, size: 20),
          ),
        ],
      ),
      body: Column(
          children: [Expanded(child: _buildBody(data)), _buildComposer()]),
    );
  }

  Widget _buildBody(Transcript? data) {
    if (_loading && data == null) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }
    if (data == null) {
      return _ErrorState(
          message: _error ?? '没有可显示的内容',
          onRetry: () => _refresh(initial: true));
    }
    if (data.isScreen) {
      return SingleChildScrollView(
        padding: const EdgeInsets.all(14),
        child: Container(
          width: double.infinity,
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
              color: const Color(0xFF272622),
              borderRadius: BorderRadius.circular(10)),
          child: SelectableText(
            data.content,
            style: const TextStyle(
                fontFamily: 'monospace',
                fontSize: 11.5,
                height: 1.45,
                color: Color(0xFFF5F4ED)),
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
            child: Text(_error!,
                style: TextStyle(color: AppTheme.danger, fontSize: 12)),
          );
        }
        final position = index - offset;
        if (position < messages.length) {
          return MessageTile(
              message: messages[position],
              pathLinks: _pathLinkActions,
              baseUrl: widget.baseUrl,
              cookieHeader: _cookieHeader);
        }
        final text = _pending[position - messages.length];
        final failed = _failed.contains(text);
        return Column(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            MessageTile(
              message:
                  ChatMessage(id: 'pending-$index', role: 'user', text: text),
              pathLinks: _pathLinkActions,
              baseUrl: widget.baseUrl,
              cookieHeader: _cookieHeader,
            ),
            if (failed)
              Padding(
                padding: const EdgeInsets.only(bottom: 6),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text('发送失败',
                        style:
                            TextStyle(fontSize: 11.5, color: AppTheme.danger)),
                    const SizedBox(width: 6),
                    TextButton(
                      key: const Key('chat-resend'),
                      onPressed: _sending ? null : () => _resend(text),
                      style: TextButton.styleFrom(
                        minimumSize: const Size(0, 28),
                        padding: const EdgeInsets.symmetric(horizontal: 8),
                        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                      ),
                      child: const Text('重发', style: TextStyle(fontSize: 12)),
                    ),
                  ],
                ),
              ),
          ],
        );
      },
    );
  }

  Widget _buildComposer() {
    if (widget.source.readOnly) {
      return Container(
        key: const Key('read-only-banner'),
        width: double.infinity,
        padding: EdgeInsets.fromLTRB(
            16, 12, 16, 12 + MediaQuery.of(context).padding.bottom),
        decoration: BoxDecoration(
            color: AppTheme.page,
            border: Border(top: BorderSide(color: AppTheme.line))),
        child: Text(
          '只读分享：可以查看内容，但不能发送消息。',
          textAlign: TextAlign.center,
          style: TextStyle(fontSize: 12.5, color: AppTheme.muted),
        ),
      );
    }
    return Container(
      padding: EdgeInsets.fromLTRB(
          12, 6, 12, 8 + MediaQuery.of(context).padding.bottom),
      decoration: BoxDecoration(
          color: AppTheme.page,
          border: Border(top: BorderSide(color: AppTheme.line))),
      child: Column(children: [
        if (_sendState.isNotEmpty)
          Align(
            alignment: Alignment.centerLeft,
            child: Padding(
              padding: const EdgeInsets.only(bottom: 3, left: 2),
              child: Text(
                _sendState,
                key: const Key('chat-send-state'),
                style: TextStyle(
                    fontSize: 11.5,
                    color: _sendStateOk ? AppTheme.green : AppTheme.danger),
              ),
            ),
          ),
        if (widget.source is PaneChatSource)
          AttachmentBar(
              items: _attachments,
              onAttach: _pickAttachments,
              onRemove: _removeAttachment),
        Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Expanded(
              child: Focus(
                onKeyEvent: (node, event) {
                  if (event is! KeyDownEvent) return KeyEventResult.ignored;
                  final key = event.logicalKey;
                  if (key != LogicalKeyboardKey.enter &&
                      key != LogicalKeyboardKey.numpadEnter) {
                    return KeyEventResult.ignored;
                  }
                  // Shift+Enter keeps the default behaviour: a newline.
                  if (HardwareKeyboard.instance.isShiftPressed) {
                    return KeyEventResult.ignored;
                  }
                  _send();
                  return KeyEventResult.handled;
                },
                child: TextField(
                  key: const Key('chat-composer'),
                  controller: _controller,
                  autofocus: !widget.embedded && _canAutoFocus,
                  onChanged: (_) => setState(() {}),
                  minLines: 1,
                  maxLines: 5,
                  textInputAction: TextInputAction.newline,
                  style: const TextStyle(fontSize: 14.5, height: 1.4),
                  decoration: const InputDecoration(
                      hintText: '继续提问…（Enter 发送，Shift+Enter 换行）'),
                ),
              ),
            ),
            const SizedBox(width: 8),
            SizedBox(
              height: 46,
              width: 46,
              child: FilledButton(
                key: Key(_showStop ? 'chat-stop' : 'chat-send'),
                onPressed: _showStop
                    ? (_stopping ? null : _stop)
                    : (_sending ? null : _send),
                style: FilledButton.styleFrom(
                  padding: EdgeInsets.zero,
                  minimumSize: const Size(46, 46),
                  shape: const CircleBorder(),
                  backgroundColor: _showStop ? AppTheme.danger : null,
                ),
                child: _stopping
                    ? const SizedBox(
                        height: 16,
                        width: 16,
                        child: CircularProgressIndicator(
                            strokeWidth: 2, color: Colors.white))
                    : _sending
                        ? const SizedBox(
                            height: 16,
                            width: 16,
                            child: CircularProgressIndicator(
                                strokeWidth: 2, color: Colors.white))
                        : Icon(
                            _showStop ? Icons.stop_rounded : Icons.arrow_upward,
                            size: _showStop ? 24 : 20),
              ),
            ),
          ],
        ),
      ]),
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
      child: Text(label,
          style: TextStyle(
              fontSize: 11, color: accent ? AppTheme.accent : AppTheme.muted)),
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
            Text(message,
                textAlign: TextAlign.center,
                style: TextStyle(color: AppTheme.muted, fontSize: 13)),
            const SizedBox(height: 12),
            OutlinedButton(onPressed: onRetry, child: const Text('重试')),
          ],
        ),
      ),
    );
  }
}

/// Create-share dialog, mirroring web/index.html's share dialog:
/// slug, expiry, optional password, allow-input toggle. Submits with
/// "创建并复制链接" and reports the full link, like web's createShare.
class _CreateShareDialog extends StatefulWidget {
  const _CreateShareDialog(
      {required this.api, required this.pane, required this.baseUrl});

  final Api api;
  final Pane pane;
  final String baseUrl;

  @override
  State<_CreateShareDialog> createState() => _CreateShareDialogState();
}

class _CreateShareDialogState extends State<_CreateShareDialog> {
  late final TextEditingController _slug;
  final TextEditingController _password = TextEditingController();
  int _expiresIn = 86400;
  bool _allowInput = true;
  String _result = '';
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _slug = TextEditingController(
        text:
            'share-${DateTime.now().millisecondsSinceEpoch.toRadixString(36)}');
  }

  @override
  void dispose() {
    _slug.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final slug = _slug.text.trim();
    if (slug.isEmpty) {
      setState(() => _result = '请填写分享路径');
      return;
    }
    // Client-side mirror of the server rule (tmux_web.py:62) and web's
    // input pattern (index.html:327): 3–48 chars, lowercase letters,
    // digits and hyphens, starting with a letter or digit.
    if (!RegExp(r'^[a-z0-9][a-z0-9-]{2,47}$').hasMatch(slug)) {
      setState(() => _result = '分享路径只能用小写字母、数字和连字符，3–48 个字符，且以字母或数字开头');
      return;
    }
    setState(() {
      _busy = true;
      _result = '创建中…';
    });
    try {
      final created = await widget.api.createShare(
        paneRef: widget.pane.ref,
        slug: slug,
        password: _password.text,
        expiresIn: _expiresIn,
        allowInput: _allowInput,
      );
      final path = '${created['url'] ?? '/share/$slug'}';
      final url = widget.baseUrl.isEmpty ? path : '${widget.baseUrl}$path';
      try {
        await Clipboard.setData(ClipboardData(text: url));
        if (!mounted) return;
        setState(() {
          _busy = false;
          _result = '已创建并复制：$url';
        });
      } catch (_) {
        if (!mounted) return;
        setState(() {
          _busy = false;
          _result = '已创建：$url';
        });
      }
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _result = '$error';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('分享会话'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('访客只能查看这一个会话，不能访问其他 pane 或终端。',
                style: TextStyle(fontSize: 12, color: AppTheme.muted)),
            const SizedBox(height: 12),
            TextField(
              key: const Key('share-slug'),
              controller: _slug,
              decoration: const InputDecoration(
                  labelText: '分享路径', hintText: 'share001'),
            ),
            const SizedBox(height: 10),
            DropdownButton<int>(
              key: const Key('share-expiry'),
              value: _expiresIn,
              isExpanded: true,
              items: const [
                DropdownMenuItem(value: 3600, child: Text('1 小时')),
                DropdownMenuItem(value: 86400, child: Text('1 天')),
                DropdownMenuItem(value: 604800, child: Text('7 天')),
                DropdownMenuItem(value: 2592000, child: Text('30 天')),
              ],
              onChanged: (value) {
                if (value != null) setState(() => _expiresIn = value);
              },
            ),
            const SizedBox(height: 10),
            TextField(
              key: const Key('share-password'),
              controller: _password,
              obscureText: true,
              decoration: const InputDecoration(labelText: '访问密码（可选）'),
            ),
            CheckboxListTile(
              key: const Key('share-allow-input'),
              contentPadding: EdgeInsets.zero,
              controlAffinity: ListTileControlAffinity.leading,
              title: const Text('允许访客继续对话（不勾选 = 只读）',
                  style: TextStyle(fontSize: 13)),
              value: _allowInput,
              onChanged: (value) {
                if (value != null) setState(() => _allowInput = value);
              },
            ),
            if (_result.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(_result,
                    style: TextStyle(fontSize: 12, color: AppTheme.muted)),
              ),
          ],
        ),
      ),
      actions: [
        TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('取消')),
        FilledButton(
          key: const Key('share-submit'),
          onPressed: _busy ? null : _submit,
          child: const Text('创建并复制链接'),
        ),
      ],
    );
  }
}
