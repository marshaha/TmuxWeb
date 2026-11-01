import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

import '../api/api_client.dart';

/// Import side of web's importConfig: hosts from a decrypted bundle,
/// skipping hosts that already exist (same hostname|port|username),
/// creating the rest with verify:false, then relinking jump chains by
/// name (ids change on import), and storing grid state for the grid
/// page (batch 6) to read later.
class ImportResult {
  ImportResult({required this.lines, required this.failures});
  final List<String> lines;
  final List<String> failures;
}

Future<ImportResult> importConfigBundle(
  Api api,
  Map<String, dynamic> bundle,
  SharedPreferences prefs,
) async {
  final lines = <String>[];
  final failures = <String>[];
  final hosts = bundle['hosts'];
  if (hosts is List && hosts.isNotEmpty) {
    final existing = await api.remoteHosts();
    final known = existing.map((host) => '${host.hostname}|${host.port}|${host.username}').toSet();
    var skipped = 0;
    final created = <MapEntry<Map<String, dynamic>, String>>[];
    for (final rawHost in hosts) {
      if (rawHost is! Map) {
        failures.add('有一条主机记录缺地址或用户名');
        continue;
      }
      final host = Map<String, dynamic>.from(rawHost);
      if (host['hostname'] == null || host['username'] == null) {
        failures.add('有一条主机记录缺地址或用户名');
        continue;
      }
      final key = '${host['hostname']}|${host['port'] ?? 22}|${host['username']}';
      if (known.contains(key)) {
        skipped += 1;
        continue;
      }
      try {
        final displayName = '${host['name'] ?? host['hostname']}';
        final saved = await api.createRemoteHost({
          'name': displayName.length > 64 ? displayName.substring(0, 64) : displayName,
          'hostname': host['hostname'],
          'port': host['port'] ?? 22,
          'username': host['username'],
          'auth_type': host['auth_type'] ?? 'agent',
          'credential': host['credential'] ?? '',
          'host_key': host['host_key'] ?? '',
          'fingerprint': host['fingerprint'] ?? '',
          'jump_spec': host['jump_spec'] ?? '',
          'verify': false,
        });
        created.add(MapEntry(host, saved.id));
        known.add(key);
      } catch (error) {
        failures.add('${host['name'] ?? host['hostname']}: $error');
      }
    }
    final byName = <String, String>{};
    for (final entry in created) {
      byName['${entry.key['name'] ?? entry.key['hostname']}'] = entry.value;
    }
    for (final entry in created) {
      final target = entry.key['jump_name'] != null ? byName[entry.key['jump_name']] : null;
      if (target == null || target == entry.value) continue;
      try {
        await api.updateRemoteHost(entry.value, {'jump_host_id': target, 'verify': false});
      } catch (error) {
        failures.add('${entry.key['name']}: 跳板链没接上（$error）');
      }
    }
    lines.add('主机：新增 ${created.length}，跳过 $skipped${failures.isNotEmpty ? '，问题 ${failures.length}' : ''}');
  }
  final grid = bundle['grid'];
  if (grid is Map && grid['panes'] != null) {
    await prefs.setString('tmux-grid-v1', jsonEncode(grid));
    lines.add('分窗布局已覆盖');
  }
  if (bundle['grid_presets'] != null) {
    await prefs.setString('tmux-grid-presets-v1', jsonEncode(bundle['grid_presets']));
    lines.add('分窗预设已覆盖');
  }
  return ImportResult(lines: lines, failures: failures);
}
