import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/models/chat_source.dart';
import 'package:tmuxweb_client/models/models.dart';
import 'package:tmuxweb_client/screens/chat_screen.dart';
import 'package:tmuxweb_client/theme.dart';
import 'package:tmuxweb_client/widgets/attachment_bar.dart';

import 'screens_test.dart' show FakeApi;

void main() {
  group('attachmentPrompt / uniqueAttachmentName / isImageFileName', () {
    test('prompt is empty without uploaded items, curly-quoted otherwise', () {
      expect(attachmentPrompt([]), '');
      expect(
        attachmentPrompt([PendingAttachment(name: 'a.png', bytes: const [1], isImage: true, uploaded: true)]),
        'user uploaded file “a.png”',
      );
      expect(
        attachmentPrompt([
          PendingAttachment(name: 'a.png', bytes: const [1], isImage: true, uploaded: true),
          PendingAttachment(name: 'b.pdf', bytes: const [2], isImage: false, uploaded: true),
          PendingAttachment(name: 'c.txt', bytes: const [3], isImage: false),
        ]),
        'user uploaded file “a.png & b.pdf”',
      );
    });

    test('unique names follow web uniqueName', () {
      expect(uniqueAttachmentName('a.png', {'b.png'}), 'a.png');
      expect(uniqueAttachmentName('a.png', {'a.png'}), 'a-2.png');
      expect(uniqueAttachmentName('a.png', {'a.png', 'a-2.png'}), 'a-3.png');
      expect(uniqueAttachmentName('README', {'README'}), 'README-2');
    });

    test('image extensions are thumbnail-eligible', () {
      expect(isImageFileName('a.png'), isTrue);
      expect(isImageFileName('a.JPG'), isTrue);
      expect(isImageFileName('a.pdf'), isFalse);
      expect(isImageFileName('a.svg'), isFalse);
    });
  });

  group('AttachmentBar widget', () {
    testWidgets('chips show name/size, image gets a thumbnail, delete reports its index', (tester) async {
      final removed = <int>[];
      // A 1x1 transparent PNG.
      final png = base64Decode(
          'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==');
      await tester.pumpWidget(MaterialApp(
        theme: AppTheme.light(),
        home: Scaffold(
          body: AttachmentBar(
            items: [
              PendingAttachment(name: 'shot.png', bytes: png, isImage: true, uploaded: true),
              PendingAttachment(name: 'doc.pdf', bytes: const [1, 2, 3], isImage: false, uploaded: true),
              PendingAttachment(name: 'slow.txt', bytes: const [1], isImage: false),
            ],
            onAttach: () {},
            onRemove: removed.add,
          ),
        ),
      ));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('chat-attach')), findsOneWidget);
      expect(find.text('存到当前目录的 upload_files'), findsOneWidget);
      expect(find.text('doc.pdf'), findsOneWidget);
      expect(find.text('3 B'), findsOneWidget);
      expect(find.text('上传中…'), findsOneWidget);
      expect(find.byKey(const Key('attachment-remove-0')), findsOneWidget);
      expect(find.byKey(const Key('attachment-remove-2')), findsOneWidget);
      // Red 22px disc with a white bold ×.
      final disc = tester.widget<Container>(find.descendant(
        of: find.byKey(const Key('attachment-remove-1')),
        matching: find.byType(Container),
      ).first);
      final decoration = disc.decoration as BoxDecoration;
      expect(decoration.shape, BoxShape.circle);
      expect(decoration.color, AppTheme.danger);

      await tester.tap(find.byKey(const Key('attachment-remove-2')));
      await tester.pump();
      expect(removed, [2]);
    });
  });

  group('ChatScreen attachments', () {
    Widget wrap(Widget child) => MaterialApp(theme: AppTheme.light(), home: child);

    Pane pane(String ref) => Pane(
          ref: ref,
          title: 'review',
          command: 'codex',
          session: 'codex',
          path: '/srv/work',
          hasTranscript: true,
          number: 7,
        );

    testWidgets('attach uploads to upload_files, send injects the prompt line, chips clear', (tester) async {
      final api = FakeApi();
      await tester.pumpWidget(wrap(ChatScreen(
        source: PaneChatSource(api: api, pane: pane('%7')),
        poll: Duration.zero,
        pickAttachments: () async => [PendingAttachment(name: 'a & b.txt', bytes: const [1, 2, 3], isImage: false)],
      )));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('chat-attach')), findsOneWidget);
      await tester.tap(find.byKey(const Key('chat-attach')));
      await tester.pumpAndSettle();
      expect(find.text('a & b.txt'), findsOneWidget);
      expect(api.uploads, ['/srv/work/upload_files/a & b.txt|3']);

      await tester.enterText(find.byKey(const Key('chat-composer')), 'hello');
      await tester.tap(find.byKey(const Key('chat-send')));
      await tester.pumpAndSettle();
      expect(api.sent, ['user uploaded file “a & b.txt”\n\nhello']);
      expect(find.text('a & b.txt'), findsNothing);
    });

    testWidgets('attachments-only send sends just the prompt line', (tester) async {
      final api = FakeApi();
      await tester.pumpWidget(wrap(ChatScreen(
        source: PaneChatSource(api: api, pane: pane('%7')),
        poll: Duration.zero,
        pickAttachments: () async => [PendingAttachment(name: 'a.txt', bytes: const [1], isImage: false)],
      )));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('chat-attach')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('chat-send')));
      await tester.pumpAndSettle();
      expect(api.sent, ['user uploaded file “a.txt”']);
    });

    testWidgets('removing a chip drops it from the composer', (tester) async {
      final api = FakeApi();
      await tester.pumpWidget(wrap(ChatScreen(
        source: PaneChatSource(api: api, pane: pane('%7')),
        poll: Duration.zero,
        pickAttachments: () async => [PendingAttachment(name: 'a.txt', bytes: const [1], isImage: false)],
      )));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('chat-attach')));
      await tester.pumpAndSettle();
      expect(find.text('a.txt'), findsOneWidget);
      await tester.tap(find.byKey(const Key('attachment-remove-0')));
      await tester.pumpAndSettle();
      expect(find.text('a.txt'), findsNothing);
    });

    testWidgets('switching panes clears pending attachments', (tester) async {
      final api = FakeApi();
      await tester.pumpWidget(wrap(ChatScreen(
        source: PaneChatSource(api: api, pane: pane('%7')),
        poll: Duration.zero,
        pickAttachments: () async => [PendingAttachment(name: 'a.txt', bytes: const [1], isImage: false)],
      )));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('chat-attach')));
      await tester.pumpAndSettle();
      expect(find.text('a.txt'), findsOneWidget);

      await tester.pumpWidget(wrap(ChatScreen(
        source: PaneChatSource(api: api, pane: pane('%8')),
        poll: Duration.zero,
        pickAttachments: () async => [],
      )));
      await tester.pumpAndSettle();
      expect(find.text('a.txt'), findsNothing);
    });
  });
}
