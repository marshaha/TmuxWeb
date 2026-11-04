import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/widgets/grid_pager.dart';

import 'screens_test.dart' show wrap;

/// Pager drag-start haptic (web's `navigator.vibrate(8)` in
/// bindPager, fired when the 380ms hold arms the drag).
///
/// The real implementation lives in `pager_haptics.dart` and needs a
/// platform vibrator, so these tests inject a recorder through
/// [GridPager.onDragStartHaptic] and assert only the firing point:
/// exactly when the hold arms the drag, never on a plain tap.
void main() {
  Future<void> pumpPager(WidgetTester tester, Future<void> Function() haptic) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(wrap(Scaffold(
      body: Stack(
        children: [
          GridPager(
            index: 1,
            total: 2,
            onUp: () {},
            onDown: () {},
            onDragStartHaptic: haptic,
          ),
        ],
      ),
    )));
    await tester.pumpAndSettle();
  }

  testWidgets('hold past 380ms fires the drag-start haptic once', (tester) async {
    var calls = 0;
    await pumpPager(tester, () async => calls++);

    final gesture = await tester.startGesture(tester.getCenter(find.byKey(const Key('grid-pager-text'))));
    await tester.pump(const Duration(milliseconds: 380));
    expect(calls, 1);

    // Moving and releasing must not fire it again.
    await gesture.moveBy(const Offset(50, 40));
    await tester.pump();
    await gesture.up();
    await tester.pump(const Duration(milliseconds: 500));
    expect(calls, 1);
  });

  testWidgets('release before 380ms never fires the haptic', (tester) async {
    var calls = 0;
    await pumpPager(tester, () async => calls++);

    final gesture = await tester.startGesture(tester.getCenter(find.byKey(const Key('grid-pager-text'))));
    await tester.pump(const Duration(milliseconds: 200));
    await gesture.moveBy(const Offset(30, 20));
    await tester.pump();
    await gesture.up();
    await tester.pump(const Duration(milliseconds: 500));
    expect(calls, 0);
  });
}
