import 'package:desktop_drop/desktop_drop.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/screens/files_screen.dart';
import 'package:tmuxweb_client/screens/terminal_screen.dart';
import 'package:xterm/xterm.dart';

import 'screens_test.dart' show FakeApi, wrap;

/// Fake upload backend that fails the second file, to prove the
/// drop stops on the first failure like web's `uploadFiles`.
class _FailingSecondUploadApi extends FakeApi {
  @override
  Future<void> uploadFile(String paneRef, String dir, String name, List<int> bytes) async {
    if (name == 'bad.txt') throw Exception('HTTP 403');
    return super.uploadFile(paneRef, dir, name, bytes);
  }
}

/// Item 4 (OS drag-and-drop upload) and item 8 (terminal
/// pinch-to-zoom font).
///
/// A real OS drop cannot be simulated in flutter test, so the drop
/// tests drive the [DropTarget] callbacks directly — the wiring the
/// screen owns — with a [DropItemFile.fromData] item, whose bytes
/// are in memory (no real file IO in the test zone). The overlay
/// state is exercised through onDragEntered/onDragExited the same
/// way.
void main() {
  final pane = Pane(ref: '%7', title: 'review', command: 'codex', session: 'codex', path: '/srv/work');

  group('desktop drag-and-drop upload', () {
    /// The test host platform is not a desktop target, so the
    /// desktop gate is opened explicitly per test. The override
    /// must be cleared at the end of the test body itself: the
    /// framework verifies foundation debug variables before
    /// tearDowns run, so an addTearDown reset lands too late.
    Future<void> pumpDesktopFiles(WidgetTester tester, FakeApi api) async {
      debugDefaultTargetPlatformOverride = TargetPlatform.linux;
      await tester.pumpWidget(wrap(FilesScreen(api: api, pane: pane)));
      await tester.pumpAndSettle();
    }

    void clearPlatformOverride() {
      debugDefaultTargetPlatformOverride = null;
    }

    DropTarget dropTarget(WidgetTester tester) =>
        tester.widget<DropTarget>(find.byKey(const Key('files-drop-target')));

    testWidgets('desktop files screen has a drop target and the web hint', (tester) async {
      await pumpDesktopFiles(tester, FakeApi());

      expect(find.byKey(const Key('files-drop-target')), findsOneWidget);
      expect(find.text('拖拽文件到此处上传'), findsOneWidget);
      // No drag in progress: no overlay.
      expect(find.byKey(const Key('files-drop-overlay')), findsNothing);
      clearPlatformOverride();
    });

    testWidgets('drag hover shows the drop overlay until the drag leaves', (tester) async {
      await pumpDesktopFiles(tester, FakeApi());

      dropTarget(tester).onDragEntered!(
        DropEventDetails(localPosition: Offset.zero, globalPosition: const Offset(400, 300)),
      );
      await tester.pump();
      expect(find.byKey(const Key('files-drop-overlay')), findsOneWidget);
      expect(find.text('松开以上传到当前目录'), findsOneWidget);

      dropTarget(tester).onDragExited!(
        DropEventDetails(localPosition: Offset.zero, globalPosition: const Offset(400, 300)),
      );
      await tester.pump();
      expect(find.byKey(const Key('files-drop-overlay')), findsNothing);
      clearPlatformOverride();
    });

    testWidgets('drop uploads through the existing upload path', (tester) async {
      final api = FakeApi();
      await pumpDesktopFiles(tester, api);

      dropTarget(tester).onDragDone!(
        DropDoneDetails(
          // On io builds XFile.name is the path basename, so the
          // name travels via path; bytes are in memory, not a file.
          files: [DropItemFile.fromData(Uint8List.fromList([104, 105]), path: 'dropped.txt')],
          localPosition: const Offset(400, 300),
          globalPosition: const Offset(400, 300),
        ),
      );
      await tester.pumpAndSettle();

      expect(api.uploads, ['/srv/work/dropped.txt|2']);
      // Web's uploadFiles always reports the count, even for one file.
      expect(find.text('已上传 1 个文件'), findsOneWidget);
      expect(find.byKey(const Key('files-drop-overlay')), findsNothing);
      clearPlatformOverride();
    });

    testWidgets('a failed upload stops the drop like web, with <name>: <error>', (tester) async {
      final api = _FailingSecondUploadApi();
      await pumpDesktopFiles(tester, api);

      dropTarget(tester).onDragDone!(
        DropDoneDetails(
          files: [
            DropItemFile.fromData(Uint8List.fromList([104, 105]), path: 'good.txt'),
            DropItemFile.fromData(Uint8List.fromList([106]), path: 'bad.txt'),
          ],
          localPosition: const Offset(400, 300),
          globalPosition: const Offset(400, 300),
        ),
      );
      await tester.pumpAndSettle();

      // Only the first file went up; no success message follows.
      expect(api.uploads, ['/srv/work/good.txt|2']);
      expect(find.text('bad.txt: Exception: HTTP 403'), findsOneWidget);
      expect(find.textContaining('已上传'), findsNothing);
      clearPlatformOverride();
    });

    testWidgets('mobile files screen has no drop target, picker button stays', (tester) async {
      debugDefaultTargetPlatformOverride = TargetPlatform.android;
      await tester.pumpWidget(wrap(FilesScreen(api: FakeApi(), pane: pane)));
      await tester.pumpAndSettle();

      expect(find.byType(DropTarget), findsNothing);
      expect(find.byKey(const Key('files-upload')), findsOneWidget);
      clearPlatformOverride();
    });
  });

  group('terminal pinch-to-zoom font', () {
    setUp(() {
      SharedPreferences.setMockInitialValues({'tmux-term-font': 13.0});
    });

    Future<void> pumpTerminal(WidgetTester tester) async {
      await tester.pumpWidget(wrap(TerminalScreen(api: FakeApi(), pane: pane)));
      await tester.pumpAndSettle();
    }

    double terminalFontSize(WidgetTester tester) =>
        tester.widget<TerminalView>(find.byType(TerminalView)).textStyle.fontSize;

    Future<double> savedFont() async =>
        (await SharedPreferences.getInstance()).getDouble(terminalFontPrefsKey)!;

    testWidgets('pinch out grows the font and persists it under tmux-term-font', (tester) async {
      await pumpTerminal(tester);
      expect(terminalFontSize(tester), 13.0);

      final first = await tester.startGesture(const Offset(300, 300));
      final second = await tester.startGesture(const Offset(400, 300));
      await second.moveBy(const Offset(100, 0));
      await tester.pump();
      await tester.pump();
      await first.up();
      await second.up();
      // xterm arms a 300ms double-tap timer on tap-up; let it fire
      // so no fake timer is still pending at teardown.
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pump();

      // Spread 100 -> 200 doubles 13 to 26, clamped to web's 22px cap.
      expect(terminalFontSize(tester), 22.0);
      expect(await savedFont(), 22.0);
    });

    testWidgets('pinch in shrinks the font to the 9px floor', (tester) async {
      await pumpTerminal(tester);

      final first = await tester.startGesture(const Offset(300, 300));
      final second = await tester.startGesture(const Offset(500, 300));
      await second.moveBy(const Offset(-100, 0));
      await tester.pump();
      await tester.pump();
      await first.up();
      await second.up();
      // xterm arms a 300ms double-tap timer on tap-up; let it fire
      // so no fake timer is still pending at teardown.
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pump();

      // Spread 200 -> 100 halves 13 to 6.5, rounded then clamped to 9.
      expect(terminalFontSize(tester), 9.0);
      expect(await savedFont(), 9.0);
    });

    testWidgets('a pinch inside the dead zone leaves the font alone', (tester) async {
      await pumpTerminal(tester);

      final first = await tester.startGesture(const Offset(300, 300));
      final second = await tester.startGesture(const Offset(400, 300));
      await second.moveBy(const Offset(2, 0));
      await tester.pump();
      await first.up();
      await second.up();
      // xterm arms a 300ms double-tap timer on tap-up; let it fire
      // so no fake timer is still pending at teardown.
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pump();

      // Web ignores ratios within 0.05 of 1 (here 1.02).
      expect(terminalFontSize(tester), 13.0);
      expect(await savedFont(), 13.0);
    });

    testWidgets('a single-finger drag does not change the font', (tester) async {
      await pumpTerminal(tester);

      final drag = await tester.startGesture(const Offset(300, 300));
      await drag.moveBy(const Offset(0, 100));
      await tester.pump();
      await drag.up();
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pump();

      expect(terminalFontSize(tester), 13.0);
      expect(await savedFont(), 13.0);
    });

    testWidgets('the +/- font buttons still work after the pinch change', (tester) async {
      await pumpTerminal(tester);

      await tester.tap(find.byKey(const Key('terminal-font-up')));
      await tester.pump();
      expect(terminalFontSize(tester), 14.0);
      expect(await savedFont(), 14.0);

      await tester.tap(find.byKey(const Key('terminal-font-down')));
      await tester.pump();
      expect(terminalFontSize(tester), 13.0);
      expect(await savedFont(), 13.0);
    });
  });
}
