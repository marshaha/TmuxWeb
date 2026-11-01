import 'dart:io';

import 'package:path_provider/path_provider.dart';

/// Synchronous writes on purpose: the bundle is small, this runs on
/// an explicit tap, and sync dart:io completes under flutter_tester's
/// fake async where async dart:io never does.
Future<String> saveFileBytes(String filename, List<int> bytes) async {
  final directory = await getApplicationDocumentsDirectory();
  final folder = Directory('${directory.path}/tmuxweb')..createSync(recursive: true);
  final file = File('${folder.path}/$filename')..writeAsBytesSync(bytes);
  return file.path;
}
