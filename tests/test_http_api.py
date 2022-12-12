import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from zlb.http_api import make_handler, read_token


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.applier = Mock()
        self.applier.apply.return_value = 'verified-digest'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.applier, 'a' * 32))
        self.server.daemon_threads = True
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    def request(self, body=b'{}', auth=True, headers=None, method='POST', path='/v1/state'):
        connection = HTTPConnection(*self.server.server_address, timeout=3)
        try:
            values = {'Authorization': 'Bearer ' + 'a' * 32} if auth else {}
            values.update(headers or {})
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_unauthenticated_request_never_reaches_applier(self):
        self.assertEqual(self.request(auth=False)[0], 401)
        self.applier.apply.assert_not_called()

    def test_digest_only_after_successful_apply(self):
        self.assertEqual(self.request(), (200, {'digest': 'verified-digest'}))
        self.applier.apply.assert_called_once_with({})

    def test_failed_apply_is_not_acknowledged_or_exposed(self):
        self.applier.apply.side_effect = RuntimeError('private command details')
        with self.assertLogs('zlb.http_api', level='ERROR'):
            self.assertEqual(self.request(), (503, {'error': 'Load balancer node apply failed; retry later'}))

    def test_bad_json_and_invalid_state_are_client_errors(self):
        self.assertEqual(self.request(body=b'{')[0], 400)
        self.applier.apply.assert_not_called()
        self.applier.apply.side_effect = ValueError('bad state')
        self.assertEqual(self.request()[0], 400)

    def test_invalid_request_sizes_and_encoding_rejected_before_apply(self):
        for headers in ({'Content-Length': '0'}, {'Content-Length': '2097153'},
                        {'Content-Length': '-1'}, {'Transfer-Encoding': 'chunked'}):
            self.assertEqual(self.request(headers=headers)[0], 400)
        self.applier.apply.assert_not_called()

    def test_liveness_is_separate_from_forwarding(self):
        self.assertEqual(self.request(method='GET', path='/healthz'), (200, {'alive': True}))
        self.assertEqual(self.request(path='/missing')[0], 404)
        self.applier.apply.assert_not_called()


class TokenTests(unittest.TestCase):
    def test_token_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token'
            for token in ('short', 'a' * 32 + ' b', 'é' * 32):
                path.write_text(token)
                with self.assertRaises(ValueError):
                    read_token(path)
            path.write_text('a' * 32 + '\n')
            self.assertEqual(read_token(path), 'a' * 32)
