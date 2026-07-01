"""§2c Local mock InformaCast Fusion server for real-path drills.

A stdlib ``http.server`` that emulates the Fusion endpoints the gateway calls
plus the escalation sink, bound to 127.0.0.1 on an ephemeral port. Staging /
drill configs point ``fusion.token_url``, ``fusion.base_url`` and
``escalation.webhook_url`` at this server, so delivery/retry/timeout behaviour
is exercised end-to-end WITHOUT any real notification leaving the host.

Switchable behaviours (per ``mode``): ``200`` (happy path), ``500``,
``429`` (with ``Retry-After``), ``401_then_200`` (first token call 401, then
200), and ``slow`` (sleeps to trigger a client read-timeout).

Endpoints: ``POST /api/token``, ``POST /api/v1/scenario-notifications``,
``GET /api/v1/scenarios/{id}``, ``POST /escalation``.
"""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockState:
    """Mutable knobs + request log shared with the running handler."""

    def __init__(self, mode: str = "200", retry_after: int = 2, slow_seconds: float = 2.0):
        self.mode = mode
        self.retry_after = retry_after
        self.slow_seconds = slow_seconds
        self.token_401_served = False
        self.requests: list[tuple[str, str]] = []  # (method, path)

    def count(self, method: str, needle: str) -> int:
        return sum(1 for m, p in self.requests if m == method and needle in p)


class _Handler(BaseHTTPRequestHandler):
    state: MockState = None  # set on the per-server subclass

    def log_message(self, *_args):  # silence stderr access logging
        pass

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code: int, obj=None, headers=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self.state.requests.append(("GET", self.path))
        if "/v1/scenarios/" in self.path:
            sid = self.path.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            return self._send(200, {
                "id": sid,
                "fields": [{"variable": "customTTS", "name": "customTTS", "id": "mock-field-id"}],
            })
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        self._read_body()
        self.state.requests.append(("POST", self.path))
        mode = self.state.mode

        if self.path.endswith("/token"):
            if mode == "401_then_200" and not self.state.token_401_served:
                self.state.token_401_served = True
                return self._send(401, {"error": "invalid_client"})
            if mode == "slow":
                time.sleep(self.state.slow_seconds)
            return self._send(200, {
                "access_token": "mock-token",
                "token_type": "bearer",
                "expires_in": 3600,
                "scope": "urn:singlewire:scenario-notifications:write",
            })

        if "escalation" in self.path:
            return self._send(204)

        if "scenario-notifications" in self.path:
            if mode == "500":
                return self._send(500, {"error": "internal"})
            if mode == "429":
                return self._send(429, {"error": "rate_limited"},
                                  {"Retry-After": str(self.state.retry_after)})
            if mode == "slow":
                time.sleep(self.state.slow_seconds)
            return self._send(200, {"events": [{"notification": {"id": "mock-notif"}}]})

        return self._send(404, {"error": "not found"})


@contextmanager
def run_mock_fusion(mode: str = "200", retry_after: int = 2, slow_seconds: float = 2.0):
    """Start the mock on 127.0.0.1:<ephemeral>. Yields ``(base_url, state)``.

    ``base_url`` is like ``http://127.0.0.1:54321``; append ``/api`` for Fusion
    base_url and ``/api/token`` for the token URL to mirror production paths.
    """
    state = MockState(mode=mode, retry_after=retry_after, slow_seconds=slow_seconds)
    handler_cls = type("_BoundHandler", (_Handler,), {"state": state})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)
