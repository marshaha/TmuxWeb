import 'package:flutter/foundation.dart';

/// Native approximation of the primary pointer kind: mobile targets
/// are touch (coarse), desktop targets are not. This file must not
/// import `package:web`; the web implementation owns that import.
bool get hasCoarsePointer {
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
