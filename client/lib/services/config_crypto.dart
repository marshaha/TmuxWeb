import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:pointycastle/export.dart';

/// The settings panel's encrypted export format, byte-compatible with
/// web/assets/settings.js so a file exported by either side imports on
/// the other:
///
///   PBKDF2-HMAC-SHA256, 250_000 iterations, 16-byte salt
///   AES-GCM-256, 12-byte IV, additional data "tmuxweb-config-encrypted"
///   envelope: {format, version: 1, algorithm: "AES-GCM",
///              kdf: {name: "PBKDF2", hash: "SHA-256", iterations, salt},
///              iv, payload} with salt/iv/payload base64.
///
/// GCM output is ciphertext followed by the 16-byte tag, which is the
/// layout both WebCrypto and pointycastle's GCMBlockCipher produce.
class ConfigCryptoException implements Exception {
  ConfigCryptoException(this.message);
  final String message;
  @override
  String toString() => message;
}

class ConfigCrypto {
  static const format = 'tmuxweb-config-encrypted';
  static const version = 1;
  static const iterations = 250000;

  static Uint8List _deriveKey(String password, Uint8List salt) {
    final derivator = PBKDF2KeyDerivator(HMac(SHA256Digest(), 64));
    derivator.init(Pbkdf2Parameters(salt, iterations, 32));
    return derivator.process(Uint8List.fromList(utf8.encode(password)));
  }

  static Uint8List _random(int length) {
    final random = Random.secure();
    return Uint8List.fromList(List.generate(length, (_) => random.nextInt(256)));
  }

  /// Encrypts [bundle] (a JSON-able map) under [password]. Salt and IV
  /// are random per call, like web's encryptBundle.
  static Map<String, dynamic> encrypt(Map<String, dynamic> bundle, String password) {
    final salt = _random(16);
    final iv = _random(12);
    final key = _deriveKey(password, salt);
    final cipher = GCMBlockCipher(AESEngine());
    cipher.init(
      true,
      AEADParameters(KeyParameter(key), 128, iv, Uint8List.fromList(utf8.encode(format))),
    );
    final plain = Uint8List.fromList(utf8.encode(jsonEncode(bundle)));
    final sealed = cipher.process(plain);
    return {
      'format': format,
      'version': version,
      'algorithm': 'AES-GCM',
      'kdf': {'name': 'PBKDF2', 'hash': 'SHA-256', 'iterations': iterations, 'salt': base64Encode(salt)},
      'iv': base64Encode(iv),
      'payload': base64Encode(sealed),
    };
  }

  /// Decrypts an envelope produced by [encrypt] or by web's
  /// encryptBundle. Errors use web's decryptBundle wording so the UI
  /// shows the same messages on both sides.
  static Map<String, dynamic> decrypt(Map<String, dynamic> envelope, String password) {
    if (envelope['format'] != format) {
      throw ConfigCryptoException('这不是 TmuxWeb 导出的配置文件');
    }
    if (envelope['version'] != version) {
      throw ConfigCryptoException('不支持的配置版本 ${envelope['version']}');
    }
    final kdf = envelope['kdf'];
    if (kdf is! Map) throw ConfigCryptoException('这不是 TmuxWeb 导出的配置文件');
    final salt = _decodeBase64(kdf['salt']);
    final iv = _decodeBase64(envelope['iv']);
    final sealed = _decodeBase64(envelope['payload']);
    final key = _deriveKey(password, salt);
    final cipher = GCMBlockCipher(AESEngine());
    cipher.init(
      false,
      AEADParameters(KeyParameter(key), 128, iv, Uint8List.fromList(utf8.encode(format))),
    );
    late Uint8List plain;
    try {
      plain = cipher.process(sealed);
    } catch (_) {
      throw ConfigCryptoException('密码不对，或者文件被改过');
    }
    try {
      final decoded = jsonDecode(utf8.decode(plain));
      if (decoded is! Map<String, dynamic>) throw const FormatException('not an object');
      return decoded;
    } catch (_) {
      throw ConfigCryptoException('文件内容不是合法 JSON');
    }
  }

  static Uint8List _decodeBase64(dynamic value) {
    try {
      return base64Decode('$value');
    } catch (_) {
      throw ConfigCryptoException('这不是 TmuxWeb 导出的配置文件');
    }
  }
}
