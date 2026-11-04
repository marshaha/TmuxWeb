/// Native targets have no browser tabs; SharesScreen falls back to its
/// in-app view (its branch already requires kIsWeb), so this is a no-op.
void openShareTab(String url) {}
