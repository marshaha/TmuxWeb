import 'models.dart';

/// In-memory cache for conversations: transcript, scroll offset and draft per
/// pane (or share). It lets the client paint a conversation the moment the user
/// switches back, then revalidate in the background.
class CachedConversation {
  CachedConversation({
    required this.key,
    required this.transcript,
    required this.savedAt,
    this.scrollOffset,
    this.stickToBottom = true,
  });

  final String key;
  Transcript transcript;
  double? scrollOffset;
  bool stickToBottom;
  DateTime savedAt;
}

class TranscriptCache {
  TranscriptCache({this.capacity = 8});

  TranscriptCache._shared() : capacity = 8;

  /// Process-wide cache used by the screens.
  static final TranscriptCache instance = TranscriptCache._shared();

  final int capacity;
  final Map<String, CachedConversation> _entries = {};
  final Map<String, String> _drafts = {};

  int get length => _entries.length;

  CachedConversation? read(String key) => _entries[key];

  bool has(String key) => _entries[key] != null;

  void write(String key, Transcript transcript) {
    final existing = _entries[key];
    _entries[key] = CachedConversation(
      key: key,
      transcript: transcript,
      savedAt: DateTime.now(),
      scrollOffset: existing?.scrollOffset,
      stickToBottom: existing?.stickToBottom ?? true,
    );
    _trim(key);
  }

  void saveScroll(String key, double offset, {required bool stickToBottom}) {
    final entry = _entries[key];
    if (entry == null) return;
    entry.scrollOffset = offset;
    entry.stickToBottom = stickToBottom;
  }

  String draft(String key) => _drafts[key] ?? '';

  void saveDraft(String key, String text) {
    if (text.isEmpty) {
      _drafts.remove(key);
    } else {
      _drafts[key] = text;
    }
  }

  void clear() {
    _entries.clear();
    _drafts.clear();
  }

  void _trim(String keep) {
    if (_entries.length <= capacity) return;
    final stale = _entries.entries.where((entry) => entry.key != keep).toList()
      ..sort((a, b) => a.value.savedAt.compareTo(b.value.savedAt));
    for (final entry in stale.take(_entries.length - capacity)) {
      _entries.remove(entry.key);
    }
  }
}
