"""Authenticated desired-state HTTP(S) endpoint and token validation."""
import hmac
import ipaddress
import json
import logging
from pathlib import Path
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger(__name__)


def make_handler(applier, token, apply_error="Load balancer node apply failed; retry later"):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10  # per socket operation

        def setup(self):
            # Bound the whole request, not just each read: a client that trickles bytes
            # would otherwise hold a connection slot forever.
            self.tls = getattr(self.server, "tls", None)
            self.deadline = threading.Timer(getattr(self.server, "request_deadline", 30), self.expire)
            self.deadline.daemon = True
            self.deadline.start()
            if self.tls:
                # Handshake here, in the connection's own thread, so a slow or hostile
                # client cannot stall the accept loop.
                try:
                    self.request.settimeout(self.timeout)
                    self.request = self.tls.wrap_socket(self.request, server_side=True)
                except BaseException:
                    self.deadline.cancel()
                    raise
            super().setup()

        def expire(self):
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def finish(self):
            self.deadline.cancel()
            try:
                super().finish()
            finally:
                if self.tls:
                    self.request.close()

        def log_message(self, format, *args):
            LOG.debug("%s %s", self.address_string(), format % args)

        def response(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.deadline.cancel()
            if self.path == "/healthz":
                self.response(200, {"alive": True})
            else:
                self.response(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/state":
                self.response(404, {"error": "not found"})
                return
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                self.response(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2 * 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid request size or transfer encoding")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete body")
                # The request is complete; applying may legitimately take longer.
                self.deadline.cancel()
                revision = applier.apply(json.loads(raw))
            except (ValueError, TypeError, KeyError) as exc:
                self.response(400, {"error": str(exc)})
                return
            except Exception:
                LOG.exception("state application failed")
                self.response(503, {"error": apply_error})
                return
            self.response(200, {"digest": revision})
    return Handler


class APIServer(ThreadingHTTPServer):
    """Threaded server with an optional TLS context, a client source allowlist and a cap
    on concurrent connections. Unauthenticated clients can no longer exhaust threads."""
    daemon_threads = True

    def __init__(self, address, handler, tls=None, allow_from=(), max_connections=16,
                 request_deadline=30):
        self.tls = tls
        self.allow_from = [ipaddress.ip_network(value) for value in allow_from]
        self.slots = threading.BoundedSemaphore(max_connections)
        self.request_deadline = request_deadline
        super().__init__(address, handler)

    def verify_request(self, request, client_address):
        source = ipaddress.ip_address(client_address[0])
        if self.allow_from and not any(source in network for network in self.allow_from):
            LOG.warning("rejected API connection from %s: not in --allow-from", source)
            return False
        return True

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            LOG.warning("rejected API connection from %s: connection limit reached", client_address[0])
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Failed handshakes and dropped connections are routine on a network listener.
        LOG.warning("API connection from %s failed", client_address[0], exc_info=LOG.isEnabledFor(logging.DEBUG))


def server_context(cert, key, client_ca=None):
    """TLS 1.2+ server context. With `client_ca`, clients must present a certificate signed by
    exactly that CA; the system trust store is deliberately not loaded."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    if client_ca:
        context.load_verify_locations(cafile=client_ca)
        context.verify_mode = ssl.CERT_REQUIRED
    return context


def read_token(path):
    token = Path(path).read_text().strip()
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("token must contain at least 32 ASCII non-whitespace characters")
    return token
