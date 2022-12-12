"""Authenticated desired-state HTTP endpoint and token validation."""
import hmac
import json
import logging
from pathlib import Path
from http.server import BaseHTTPRequestHandler

LOG = logging.getLogger(__name__)


def make_handler(applier, token, apply_error="Load balancer node apply failed; retry later"):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def response(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
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


def read_token(path):
    token = Path(path).read_text().strip()
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("token must contain at least 32 ASCII non-whitespace characters")
    return token
