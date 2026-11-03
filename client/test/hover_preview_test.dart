import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/widgets/pane_hover_preview.dart';

void main() {
  Future<PanePreviewData?> load(String paneRef) async =>
      PanePreviewData(role: 'assistant', text: '最后一条：$paneRef 完成', time: '2 分钟前');

  Widget app({Duration delay = const Duration(milliseconds: 200), Future<PanePreviewData?> Function(String)? loader}) {
    return MaterialApp(
      home: Scaffold(
        body: Align(
          alignment: Alignment.topLeft,
          child: SizedBox(
            width: 200,
            child: PaneHoverPreview(
              key: const Key('hover'),
              paneRef: '%7',
              delay: delay,
              loadPreview: loader ?? load,
              child: Container(key: const Key('row'), height: 40, color: Colors.white,
                  child: const Text('pane 0 · codex')),
            ),
          ),
        ),
      ),
    );
  }

  testWidgets('停下指针才弹预览，移开就收起来', (tester) async {
    await tester.pumpWidget(app());
    final gesture = await tester.createGesture(kind: PointerDeviceKind.mouse);
    await gesture.addPointer(location: Offset.zero);
    addTearDown(gesture.removePointer);

    await gesture.moveTo(tester.getCenter(find.byKey(const Key('row'))));
    await tester.pump();
    // 还没到延迟：没有面板
    expect(find.byKey(const Key('pane-hover-preview')), findsNothing);

    await tester.pump(const Duration(milliseconds: 260));
    await tester.pump();
    expect(find.byKey(const Key('pane-hover-preview')), findsOneWidget);
    expect(find.textContaining('最后一条：%7'), findsOneWidget);

    await gesture.moveTo(const Offset(600, 400));
    await tester.pump();
    expect(find.byKey(const Key('pane-hover-preview')), findsNothing);
  });

  testWidgets('取不到内容就不弹空面板', (tester) async {
    await tester.pumpWidget(app(loader: (paneRef) async => null));
    final gesture = await tester.createGesture(kind: PointerDeviceKind.mouse);
    await gesture.addPointer(location: Offset.zero);
    addTearDown(gesture.removePointer);
    await gesture.moveTo(tester.getCenter(find.byKey(const Key('row'))));
    await tester.pump(const Duration(milliseconds: 260));
    await tester.pump();
    expect(find.byKey(const Key('pane-hover-preview')), findsNothing);
  });

  testWidgets('delay 为 0（设置里关闭）时永远不弹', (tester) async {
    await tester.pumpWidget(app(delay: Duration.zero));
    final gesture = await tester.createGesture(kind: PointerDeviceKind.mouse);
    await gesture.addPointer(location: Offset.zero);
    addTearDown(gesture.removePointer);
    await gesture.moveTo(tester.getCenter(find.byKey(const Key('row'))));
    await tester.pump(const Duration(seconds: 3));
    expect(find.byKey(const Key('pane-hover-preview')), findsNothing);
  });

  test('设置里的秒数换算成延迟', () {
    expect(PaneHoverPreview.durationFromPref(null), const Duration(seconds: 2));
    expect(PaneHoverPreview.durationFromPref('off'), Duration.zero);
    expect(PaneHoverPreview.durationFromPref('1'), const Duration(seconds: 1));
    expect(PaneHoverPreview.durationFromPref('3'), const Duration(seconds: 3));
  });
}
