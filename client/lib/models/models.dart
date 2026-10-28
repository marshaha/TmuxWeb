/// Wire models for the TmuxWeb HTTP API.
library;

class Pane {
  Pane({
    required this.ref,
    required this.title,
    required this.command,
    this.hostId = 'localhost',
    this.session = '',
    this.window = '',
    this.windowName = '',
    this.index = '',
    this.path = '',
    this.provider,
    this.active = false,
    this.hasTranscript = false,
  });

  /// Fully qualified pane reference: `%7` locally, `abc123~%7` for a remote host.
  final String ref;
  final String title;
  final String command;
  final String hostId;
  final String session;
  final String window;
  final String windowName;
  final String index;
  final String path;
  final String? provider;
  final bool active;
  final bool hasTranscript;

  String get label => title.isNotEmpty ? title : (command.isNotEmpty ? command : ref);

  factory Pane.fromJson(Map<String, dynamic> json, {String hostId = 'localhost'}) {
    final caps = json['capabilities'];
    return Pane(
      ref: '${json['id'] ?? json['pane_id'] ?? ''}',
      title: '${json['title'] ?? ''}',
      command: '${json['command'] ?? ''}',
      hostId: '${json['host_id'] ?? hostId}',
      session: '${json['session'] ?? ''}',
      window: '${json['window'] ?? ''}',
      windowName: '${json['window_name'] ?? ''}',
      index: '${json['index'] ?? ''}',
      path: '${json['path'] ?? ''}',
      provider: ((json['provider'] as String?) ?? '').isEmpty ? null : json['provider'] as String,
      active: json['active'] == true,
      hasTranscript: caps is Map ? caps['transcript'] == true : json['provider'] != null,
    );
  }
}

class Host {
  Host({required this.id, required this.name, required this.kind, required this.status, required this.panes});

  final String id;
  final String name;
  final String kind;
  final String status;
  final List<Pane> panes;

  factory Host.fromJson(Map<String, dynamic> json) {
    final hostId = '${json['id'] ?? 'localhost'}';
    final rawPanes = (json['panes'] as List?) ?? const [];
    return Host(
      id: hostId,
      name: '${json['name'] ?? hostId}',
      kind: '${json['kind'] ?? 'local'}',
      status: '${json['status'] ?? 'online'}',
      panes: rawPanes
          .whereType<Map>()
          .map((pane) => Pane.fromJson(Map<String, dynamic>.from(pane), hostId: hostId))
          .toList(),
    );
  }
}

class ChatMessage {
  ChatMessage({required this.id, required this.role, required this.text, this.timestamp = ''});

  final String id;
  final String role;
  final String text;
  final String timestamp;

  bool get isUser => role == 'user';

  factory ChatMessage.fromJson(Map<String, dynamic> json) => ChatMessage(
        id: '${json['id'] ?? ''}',
        role: '${json['role'] ?? 'assistant'}',
        text: '${json['text'] ?? ''}',
        timestamp: '${json['timestamp'] ?? ''}',
      );
}

class Transcript {
  Transcript({
    required this.mode,
    this.provider,
    this.sessionId,
    this.messages = const [],
    this.content = '',
    this.mtime = 0,
    this.readOnly = false,
    this.waiting = false,
    this.waitingSince,
  });

  /// True while the agent has not answered the last user message yet, so the
  /// composer can offer a stop button.
  final bool waiting;
  final Object? waitingSince;

  final String mode; // transcript | screen
  final String? provider;
  final String? sessionId;
  final List<ChatMessage> messages;

  /// Raw terminal text for screen-mode panes (shell panes without a transcript).
  final String content;
  final Object mtime;
  final bool readOnly;

  bool get isScreen => mode == 'screen';

  factory Transcript.fromJson(Map<String, dynamic> json, {bool readOnly = false}) {
    final messages = ((json['messages'] as List?) ?? const [])
        .whereType<Map>()
        .map((item) => ChatMessage.fromJson(Map<String, dynamic>.from(item)))
        .toList();
    return Transcript(
      mode: '${json['mode'] ?? 'transcript'}',
      provider: ((json['provider'] as String?) ?? '').isEmpty ? null : json['provider'] as String,
      sessionId: json['session_id'] == null ? null : '${json['session_id']}',
      messages: messages,
      content: '${json['content'] ?? ''}',
      mtime: json['mtime'] ?? 0,
      readOnly: readOnly,
      waiting: json['waiting'] == true,
      waitingSince: json['waiting_since'],
    );
  }
}

class ShareMeta {
  ShareMeta({
    required this.slug,
    required this.title,
    required this.mode,
    required this.allowInput,
    this.provider,
    this.passwordProtected = false,
  });

  final String slug;
  final String title;
  final String mode;
  final String? provider;
  final bool allowInput;
  final bool passwordProtected;

  factory ShareMeta.fromJson(Map<String, dynamic> json) => ShareMeta(
        slug: '${json['slug'] ?? ''}',
        title: '${json['title'] ?? '共享会话'}',
        mode: '${json['mode'] ?? 'transcript'}',
        provider: ((json['provider'] as String?) ?? '').isEmpty ? null : json['provider'] as String,
        allowInput: json['allow_input'] == true,
        passwordProtected: json['password_protected'] == true,
      );
}

/// A remote SSH host as stored by the server (`/api/remote-hosts`).
class RemoteHost {
  RemoteHost({
    required this.id,
    required this.name,
    required this.hostname,
    required this.port,
    required this.username,
    required this.authType,
    this.fingerprint = '',
  });

  final String id;
  final String name;
  final String hostname;
  final int port;
  final String username;
  final String authType; // password | key | agent
  final String fingerprint;

  bool get hasPin => fingerprint.isNotEmpty;

  factory RemoteHost.fromJson(Map<String, dynamic> json) => RemoteHost(
        id: '${json['id'] ?? ''}',
        name: '${json['name'] ?? ''}',
        hostname: '${json['hostname'] ?? ''}',
        port: int.tryParse('${json['port'] ?? 22}') ?? 22,
        username: '${json['username'] ?? ''}',
        authType: '${json['auth_type'] ?? 'password'}',
        fingerprint: '${json['fingerprint'] ?? ''}',
      );
}
