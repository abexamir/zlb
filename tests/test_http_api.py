import json
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from zlb.controller import LoadBalancerNode
from zlb.http_api import APIServer, make_handler, read_token, server_context
from zlb.model import digest


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.applier = Mock()
        self.applier.apply.return_value = 'verified-digest'
        self.server = APIServer(('127.0.0.1', 0), make_handler(self.applier, 'a' * 32))
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


def serve(test, applier, **options):
    server = APIServer(('127.0.0.1', 0), make_handler(applier, 'a' * 32), **options)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    test.addCleanup(lambda: (server.shutdown(), server.server_close(), worker.join()))
    return server


class LimitTests(unittest.TestCase):
    def setUp(self):
        self.applier = Mock()
        self.applier.apply.return_value = 'verified-digest'

    def post(self, server):
        connection = HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.request('POST', '/v1/state', body=b'{}', headers={'Authorization': 'Bearer ' + 'a' * 32})
            return connection.getresponse().status
        finally:
            connection.close()

    def test_source_allowlist_closes_other_clients_before_reading(self):
        server = serve(self, self.applier, allow_from=['10.0.0.0/8'])
        with self.assertLogs('zlb.http_api', level='WARNING'), self.assertRaises((ConnectionError, OSError)):
            self.post(server)
        self.applier.apply.assert_not_called()
        self.assertEqual(self.post(serve(self, self.applier, allow_from=['127.0.0.0/8'])), 200)

    def test_connection_cap_rejects_excess_clients_then_recovers(self):
        server = serve(self, self.applier, max_connections=2)
        idle = [socket.create_connection(server.server_address) for _ in range(2)]
        time.sleep(0.2)
        with self.assertLogs('zlb.http_api', level='WARNING'), self.assertRaises((ConnectionError, OSError)):
            self.post(server)
        for connection in idle:
            connection.close()
        deadline = time.monotonic() + 5
        while True:
            try:
                self.assertEqual(self.post(server), 200)
                break
            except (ConnectionError, OSError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def test_trickling_client_is_cut_off_at_request_deadline(self):
        server = serve(self, self.applier, request_deadline=0.5)
        connection = socket.create_connection(server.server_address)
        self.addCleanup(connection.close)
        connection.sendall(b'POST /v1/state HTTP/1.1\r\n')
        connection.settimeout(3)
        started = time.monotonic()
        self.assertEqual(connection.recv(1024), b'')
        self.assertLess(time.monotonic() - started, 2.5)
        self.applier.apply.assert_not_called()


def settle(logs, count):
    # Handshake failures are logged by the connection thread, possibly after the client
    # has already seen the TLS alert.
    deadline = time.monotonic() + 3
    while len(logs.records) < count and time.monotonic() < deadline:
        time.sleep(0.05)


def openssl(*args):
    subprocess.run(['openssl', *args], check=True, capture_output=True)


@unittest.skipUnless(shutil.which('openssl'), 'openssl is needed to create test certificates')
class TLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        d = Path(cls.directory.name)
        for name in ('ca', 'rogue'):
            openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', f'/CN={name}',
                    '-keyout', d / f'{name}.key', '-out', d / f'{name}.crt',
                    '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
        for name, ca, usage, san in (('server', 'ca', 'serverAuth', 'IP:127.0.0.1'),
                                     ('client', 'ca', 'clientAuth', 'DNS:controller'),
                                     ('intruder', 'rogue', 'clientAuth', 'DNS:controller')):
            (d / f'{name}.ext').write_text(f'subjectAltName={san}\nextendedKeyUsage={usage}\n'
                                           'keyUsage=critical,digitalSignature,keyEncipherment\n'
                                           'authorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n')
            openssl('req', '-newkey', 'rsa:2048', '-nodes', '-subj', f'/CN={name}',
                    '-keyout', d / f'{name}.key', '-out', d / f'{name}.csr')
            openssl('x509', '-req', '-in', d / f'{name}.csr', '-CA', d / f'{ca}.crt', '-CAkey', d / f'{ca}.key',
                    '-CAcreateserial', '-days', '1', '-extfile', d / f'{name}.ext', '-out', d / f'{name}.crt')
        (d / 'token').write_text('a' * 32)
        cls.d = d

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.applier = Mock()
        self.applier.apply.side_effect = digest

    def server(self, client_ca=False):
        d = self.d
        context = server_context(d / 'server.crt', d / 'server.key', d / 'ca.crt' if client_ca else None)
        return serve(self, self.applier, tls=context)

    def node(self, server, client=None):
        d = self.d
        url = 'https://%s:%d' % server.server_address
        certificate = (d / f'{client}.crt', d / f'{client}.key') if client else (None, None)
        return LoadBalancerNode(url, d / 'token', d / 'ca.crt', *certificate)

    def test_controller_pushes_over_verified_https(self):
        server = self.server()
        state = {'version': 2, 'listeners': []}
        self.node(server).apply(state)
        self.applier.apply.assert_called_once_with(state)

    def test_plaintext_and_untrusted_server_rejected(self):
        server = self.server()
        with self.assertLogs('zlb.http_api', level='WARNING') as logs:
            with self.assertRaises((ConnectionError, OSError)):
                connection = HTTPConnection(*server.server_address, timeout=3)
                connection.request('GET', '/healthz')
                connection.getresponse()
            settle(logs, 1)
        untrusted = LoadBalancerNode('https://%s:%d' % server.server_address, self.d / 'token', self.d / 'rogue.crt')
        with self.assertRaises(OSError):
            untrusted.apply({'version': 2, 'listeners': []})
        self.applier.apply.assert_not_called()

    def test_mutual_tls_requires_client_certificate_from_configured_ca(self):
        server = self.server(client_ca=True)
        with self.assertLogs('zlb.http_api', level='WARNING') as logs:
            for client in (None, 'intruder'):
                with self.subTest(client=client), self.assertRaises(OSError):
                    self.node(server, client).apply({'version': 2, 'listeners': []})
            settle(logs, 2)
        self.assertEqual(len(logs.records), 2)
        self.applier.apply.assert_not_called()
        self.node(server, 'client').apply({'version': 2, 'listeners': []})
        self.applier.apply.assert_called_once()

    def test_mutual_tls_ignores_system_trust_store(self):
        context = server_context(self.d / 'server.crt', self.d / 'server.key', self.d / 'ca.crt')
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual([c['subject'] for c in context.get_ca_certs()], [((('commonName', 'ca'),),)])
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)


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
