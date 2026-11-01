import 'package:flutter_test/flutter_test.dart';
import 'package:tmuxweb_client/services/config_crypto.dart';

void main() {
  test('decrypts a bundle sealed by the WebCrypto-shaped format', () {
    // Envelope produced with Python's cryptography library using the
    // exact web parameters (PBKDF2-SHA256 250k, AES-GCM-256, AAD
    // "tmuxweb-config-encrypted"), so this proves the Dart side reads
    // files web exported, not just its own.
    const envelope = {
      'format': 'tmuxweb-config-encrypted',
      'version': 1,
      'algorithm': 'AES-GCM',
      'kdf': {
        'name': 'PBKDF2',
        'hash': 'SHA-256',
        'iterations': 250000,
        'salt': 'AAECAwQFBgcICQoLDA0ODw==',
      },
      'iv': 'AAECAwQFBgcICQoL',
      'payload':
          '7Gm4vkHmRQ+GkHWLnpXE2Zt/V5wyLG6TprEy43NFCz5RMgzyi71pTPv39/UdfU1iZZtK9ihfM6FK2qVLjiAt/LSrG0ghDiNBwOdLebzbc5ezv7tgf3klBPkGmfO+fP015+Nz1d/4BtIT0g==',
    };
    final bundle = ConfigCrypto.decrypt(envelope, 'vector-password');
    expect(bundle['app'], 'TmuxWeb');
    expect(bundle['scope'], 'hosts');
    expect((bundle['hosts'] as List).single['hostname'], '10.0.0.8');
  });

  test('encrypt then decrypt round-trips, and a wrong password fails in web wording', () {
    final envelope = ConfigCrypto.encrypt({'app': 'TmuxWeb', 'hosts': []}, 'correct horse');
    expect(envelope['format'], 'tmuxweb-config-encrypted');
    expect(ConfigCrypto.decrypt(envelope, 'correct horse')['app'], 'TmuxWeb');
    expect(
      () => ConfigCrypto.decrypt(envelope, 'wrong password'),
      throwsA(isA<ConfigCryptoException>().having((e) => e.message, 'message', '密码不对，或者文件被改过')),
    );
    expect(
      () => ConfigCrypto.decrypt({'format': 'other'}, 'x'),
      throwsA(isA<ConfigCryptoException>().having((e) => e.message, 'message', '这不是 TmuxWeb 导出的配置文件')),
    );
  });
}
