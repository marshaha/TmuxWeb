import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:tmuxweb_client/api/api_client.dart';

/// Console-sidebar API contracts (web console: index.html loadPanes /
/// refreshHost, recents.js clear + rename-favorite): pins limit,
/// hosts refresh flag, and the per-pane clear loop. renameHomeFavorite
/// already existed and is covered by api_client_test.dart, so it is
/// asserted here only as a request-shape regression pin.
void main() {
  group('console sidebar API contracts', () {
    test('pins sends ?limit=12 by default and honors an explicit limit', () async {
      final requests = <http.Request>[];
      final client = ApiClient(baseUrl: 'http://127.0.0.1', httpClient: MockClient((request) async {
        requests.add(request);
        return http.Response('{"recent":[],"pinned":[]}', 200);
      }));
      await client.pins();
      await client.pins(limit: 5);
      expect(requests.map((request) => request.url.toString()).toList(), [
        'http://127.0.0.1/api/home/recent?limit=12',
        'http://127.0.0.1/api/home/recent?limit=5',
      ]);
    });

    test('hosts sends ?refresh=1 only when forced', () async {
      final requests = <http.Request>[];
      final client = ApiClient(baseUrl: 'http://127.0.0.1', httpClient: MockClient((request) async {
        requests.add(request);
        return http.Response('{"hosts":[]}', 200);
      }));
      await client.hosts();
      await client.hosts(refresh: true);
      expect(requests.map((request) => request.url.toString()).toList(), [
        'http://127.0.0.1/api/hosts',
        'http://127.0.0.1/api/hosts?refresh=1',
      ]);
    });

    test('clearUnpinnedRecents DELETEs every unpinned pane and skips pinned rows', () async {
      final requests = <http.Request>[];
      final client = ApiClient(baseUrl: 'http://127.0.0.1', httpClient: MockClient((request) async {
        requests.add(request);
        if (request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'recent': [
                {'pane': '%7'},
                {'pane': 'abc~%0'},
              ],
              'pinned': [
                {'pane': '%9', 'label': 'kept'},
              ],
            }),
            200,
          );
        }
        return http.Response('{}', 200);
      }));
      await client.clearUnpinnedRecents();
      expect(requests.map((request) => '${request.method} ${request.url.path}').toList(), [
        'GET /api/home/recent',
        'DELETE /api/home/recent/%257',
        'DELETE /api/home/recent/abc~%250',
      ]);
    });

    test('clearUnpinnedRecents tolerates a single failing DELETE', () async {
      final deletes = <String>[];
      final client = ApiClient(baseUrl: 'http://127.0.0.1', httpClient: MockClient((request) async {
        if (request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'recent': [
                {'pane': '%7'},
                {'pane': '%8'},
              ],
              'pinned': [],
            }),
            200,
          );
        }
        deletes.add(request.url.path);
        if (request.url.path.contains('%257')) {
          return http.Response('{"detail":"gone"}', 404);
        }
        return http.Response('{}', 200);
      }));
      await client.clearUnpinnedRecents();
      expect(deletes, ['/api/home/recent/%257', '/api/home/recent/%258']);
    });

    test('renameHomeFavorite PATCHes the favorite title', () async {
      final requests = <http.Request>[];
      final client = ApiClient(baseUrl: 'http://127.0.0.1', httpClient: MockClient((request) async {
        requests.add(request);
        return http.Response('{}', 200);
      }));
      await client.renameHomeFavorite('%7', 'My agent');
      expect(requests.single.method, 'PATCH');
      expect(requests.single.url.path, '/api/home/favorites/%257');
      expect(jsonDecode(requests.single.body), {'title': 'My agent'});
    });
  });
}
