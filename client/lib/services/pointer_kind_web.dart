import 'package:web/web.dart' as web;

/// Web's exact rule: the browser's primary pointer kind, via
/// `matchMedia('(pointer: coarse)')`. Coarse means touch; anything
/// else (fine, none) means a mouse-style pointer. This is the only
/// file in the app that may import `package:web` for this query.
bool get hasCoarsePointer =>
    web.window.matchMedia('(pointer: coarse)').matches;
