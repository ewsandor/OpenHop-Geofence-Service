"""Fakes shared by the offline test suite (no network, no hardware)."""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openhop_geofence.gpsd import Fix, GpsStatus
from openhop_geofence.openhop import (
    OpenHopAuthError,
    OpenHopProtocolError,
    OpenHopState,
    OpenHopUnavailable,
    RadioSettings,
)
from openhop_geofence.state import SafeModeLatch, TxLockout

# Keep test output readable; failures still show assertion details.
logging.getLogger("openhop_geofence").addHandler(logging.NullHandler())
logging.getLogger("openhop_geofence").propagate = False


class FakeClock:
    """Controllable monotonic and wall clocks."""

    def __init__(self, start: float = 1000.0, wall: float = 1_750_000_000.0):
        self.t = start
        self.w = wall

    def __call__(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.w

    def advance(self, seconds: float) -> None:
        self.t += seconds
        self.w += seconds


class FakeGps:
    """Stands in for GpsdReader: tests set or clear the fix directly."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.fix: Fix | None = None
        self.connected = True
        self.error: str | None = None

    def set(self, lat: float | None, lon: float | None = None) -> None:
        self.fix = None if lat is None else Fix(lat, lon, 3, self.clock(), None)

    def current_fix(self) -> Fix | None:
        if self.fix is not None:
            self.fix = Fix(self.fix.lat, self.fix.lon, 3, self.clock(), None)
        return self.fix

    def status(self) -> GpsStatus:
        return GpsStatus(self.connected, self.fix, self.clock() if self.fix else None, self.error)


class FakeOpenHop:
    """In-memory OpenHop client with injectable failures."""

    def __init__(self, mode: str = "monitor"):
        self.mode = mode
        self.writes: list[str] = []
        self.fail: Exception | None = None
        self.write_fail: Exception | None = None
        self.radio = RadioSettings(906_875_000, 250_000, 10, 5, False)
        self.companions: list[dict] = []

    def read_state(self) -> OpenHopState:
        if self.fail:
            raise self.fail
        return OpenHopState(self.mode, self.radio)

    def set_mode(self, mode: str) -> None:
        if self.fail:
            raise self.fail
        if self.write_fail:
            raise self.write_fail
        self.writes.append(mode)
        self.mode = mode

    def list_companions(self) -> list[dict]:
        return self.companions


UNAVAILABLE = OpenHopUnavailable("connection refused")
AUTH = OpenHopAuthError("HTTP 401")
PROTOCOL = OpenHopProtocolError("bad response")


class MemStore:
    """In-memory SafeModeStore; ``fail`` simulates a persistence error."""

    def __init__(self, latch: SafeModeLatch | None = None):
        self.latch = latch or SafeModeLatch()
        self.fail = False
        self.saves: list[SafeModeLatch] = []

    def load(self) -> SafeModeLatch:
        return self.latch

    def save(self, latch: SafeModeLatch) -> None:
        if self.fail:
            raise OSError("read-only file system")
        self.latch = latch
        self.saves.append(latch)


class MemTxLockoutStore:
    """In-memory operator lockout store with injectable persistence failures."""

    def __init__(self, lockout: TxLockout | None = None):
        self.lockout = lockout or TxLockout()
        self.fail = False
        self.saves: list[TxLockout] = []

    def load(self) -> TxLockout:
        return self.lockout

    def save(self, lockout: TxLockout) -> None:
        if self.fail:
            raise OSError("read-only file system")
        self.lockout = lockout
        self.saves.append(lockout)


class RecordingNotifier:
    under_systemd = False

    def __init__(self):
        self.messages: list[str] = []

    def ready(self):
        self.messages.append("READY=1")

    def watchdog(self):
        self.messages.append("WATCHDOG=1")

    def stopping(self):
        self.messages.append("STOPPING=1")

    def status(self, text):
        self.messages.append("STATUS=" + text)


# ---------------------------------------------------------------------------
# Network fakes for integration tests
# ---------------------------------------------------------------------------


class FakeOpenHopServer:
    """Minimal HTTP server implementing /api/stats and /api/set_mode."""

    def __init__(self, mode: str = "monitor", api_key: str | None = "secret"):
        self.mode = mode
        self.api_key = api_key
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _auth(self) -> bool:
                if outer.api_key and self.headers.get("X-API-Key") != outer.api_key:
                    self._reply(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                outer.requests.append(("GET", self.path))
                if not self._auth():
                    return
                if self.path == "/api/stats":
                    self._reply(200, {"config": {
                        "repeater": {"mode": outer.mode},
                        "radio": {"frequency": 906875000, "bandwidth": 250000,
                                  "spreading_factor": 10, "coding_rate": 5},
                    }})
                else:
                    self._reply(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(("POST", self.path))
                if not self._auth():
                    return
                if self.path == "/api/set_mode" and body.get("mode") in ("forward", "monitor", "no_tx"):
                    outer.mode = body["mode"]
                    self._reply(200, {"success": True, "mode": outer.mode, "persisted": True})
                else:
                    self._reply(200, {"success": False, "error": "invalid mode"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeGpsdServer:
    """TCP server that streams one TPV report per second to each client."""

    def __init__(self, lat: float, lon: float, mode: int = 3):
        self.report = {"class": "TPV", "mode": mode, "lat": lat, "lon": lon}
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()

    def _client(self, conn: socket.socket) -> None:
        with conn:
            conn.recv(1024)  # ?WATCH command
            while not self._stop.wait(0.2):
                try:
                    conn.sendall((json.dumps(self.report) + "\n").encode())
                except OSError:
                    return

    def _accept(self) -> None:
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def __enter__(self):
        threading.Thread(target=self._accept, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.sock.close()
