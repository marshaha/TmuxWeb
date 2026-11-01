import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tmuxweb_client/services/config_crypto.dart';
import 'package:tmuxweb_client/services/config_import.dart';

import 'screens_test.dart' show FakeApi;

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  test('import skips existing hosts, creates the rest, relinks jumps by name', () async {
    final api = FakeApi();
    final prefs = await SharedPreferences.getInstance();
    final bundle = ConfigCrypto.decrypt(
      ConfigCrypto.encrypt({
        'app': 'TmuxWeb',
        'scope': 'all',
        'hosts': [
          // Already on the server (FakeApi remoteHosts): skipped.
          {'name': 'ctyun', 'hostname': '100.123.123.8', 'port': 22, 'username': 'mars'},
          {'name': 'newhost', 'hostname': '10.0.0.5', 'port': 22, 'username': 'mars', 'jump_name': 'bastion'},
          {'name': 'bastion', 'hostname': '10.0.0.6', 'port': 22, 'username': 'mars'},
          {'name': 'broken'},
        ],
        'grid': {
          'panes': [
            {'id': 'x'},
          ],
        },
        'grid_presets': {'a': 1},
      }, 'import-password'),
      'import-password',
    );

    final result = await importConfigBundle(api, bundle, prefs);
    expect(result.lines, contains('主机：新增 2，跳过 1，问题 1'));
    expect(result.lines, contains('分窗布局已覆盖'));
    expect(result.lines, contains('分窗预设已覆盖'));
    expect(result.failures, hasLength(1));
    // Two creates plus one jump relink.
    expect(api.savedHostPayloads, hasLength(3));
    expect(api.savedHostPayloads.last['jump_host_id'], 'new-bastion');
    expect(api.savedHostPayloads.last['verify'], isFalse);
    expect(jsonDecode(prefs.getString('tmux-grid-v1')!)['panes'], hasLength(1));
    expect(prefs.getString('tmux-grid-presets-v1'), isNotNull);
  });

  test('import with nothing importable reports so', () async {
    final api = FakeApi();
    final prefs = await SharedPreferences.getInstance();
    final result = await importConfigBundle(api, {'app': 'TmuxWeb'}, prefs);
    expect(result.lines, isEmpty);
    expect(api.savedHostPayloads, isEmpty);
  });
}
