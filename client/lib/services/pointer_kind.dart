/// Whether the primary pointer is coarse (touch).
///
/// Web's rule (index.html / preview.html) queries the browser directly
/// with `matchMedia('(pointer: coarse)')`; native targets have no such
/// query, so the native implementation approximates it from the target
/// platform (iOS / Android / Fuchsia are touch, desktop is not).
/// The conditional export mirrors the existing
/// `services/app_socket.dart` / `services/save_file.dart` pattern
/// (`dart.library.js_interop` selects the web implementation), and
/// `package:web` is imported only from the web implementation — never
/// from shared or native code — so native builds never load it.
///
/// Callers never branch on the platform themselves: [hasCoarsePointer]
/// is the single answer on every target. Under `flutter test` the
/// native implementation runs, so tests drive it with
/// `debugDefaultTargetPlatformOverride`; the web `matchMedia` path
/// cannot run under `flutter test` and is therefore untested here.
library;

export 'pointer_kind_io.dart'
    if (dart.library.js_interop) 'pointer_kind_web.dart';
