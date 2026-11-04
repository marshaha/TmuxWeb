import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:vibration/vibration.dart';

/// The pager's drag-start haptic, mirroring web's bindPager
/// (grid.html: `if(navigator.vibrate)navigator.vibrate(8)`), fired
/// when the 380ms hold arms the drag.
///
/// Platform truth: Android and the web target take the exact 8ms
/// (the vibration plugin calls `navigator.vibrate` on web and
/// `VibrationEffect` on Android). iOS has no duration-controlled
/// vibration, so a light impact is the platform equivalent, not an
/// exact 8ms buzz. Desktop has no vibrator, so the `hasVibrator`
/// guard makes this a no-op there.
///
/// Never throws: a missing plugin, a denied capability, or a
/// platform without a vibrator must not break the drag.
Future<void> vibratePagerDragStart() async {
  try {
    if (defaultTargetPlatform == TargetPlatform.iOS) {
      await HapticFeedback.lightImpact();
      return;
    }
    if (await Vibration.hasVibrator()) {
      await Vibration.vibrate(duration: 8);
    }
  } catch (_) {
    // Haptics are best-effort; the drag must never fail for them.
  }
}
