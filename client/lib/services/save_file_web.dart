import 'dart:js_interop';
import 'dart:typed_data';

import 'package:web/web.dart' as web;

/// Browser download: a Blob URL behind a synthetic anchor click.
/// Synchronous DOM calls, so this completes immediately like the
/// native sync write; the returned name is what the caller shows.
Future<String> saveFileBytes(String filename, List<int> bytes) async {
  final blob = web.Blob(
    [Uint8List.fromList(bytes).buffer.toJS].toJS,
    web.BlobPropertyBag(type: 'application/octet-stream'),
  );
  final url = web.URL.createObjectURL(blob);
  final anchor = web.HTMLAnchorElement()
    ..href = url
    ..download = filename;
  web.document.body!.append(anchor);
  anchor.click();
  anchor.remove();
  web.URL.revokeObjectURL(url);
  return filename;
}
