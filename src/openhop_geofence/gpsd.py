"""gpsd client using the standard TCP JSON watch protocol.

The reader keeps the last validated TPV report. It never opens the receiver
device, parses NMEA, or sets the system clock. Live deadlines use monotonic
receipt time; reported timestamps are checked only when the system clock is
trusted.
"""

from __future__ import annotations

import json
import logging
import math
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .zones import valid_lat_lon

log = logging.getLogger(__name__)

MAX_LINE_BYTES = 64 * 1024


@dataclass(frozen=True)
class Fix:
    """A validated position report."""

    lat: float
    lon: float
    mode: int  # 2 = 2D, 3 = 3D
    received_monotonic: float
    reported_utc: float | None = None
    speed_mps: float | None = None
    track_deg: float | None = None
    eph_m: float | None = None
    device: str | None = None


@dataclass(frozen=True)
class GpsStatus:
    connected: bool
    fix: Fix | None
    last_report_monotonic: float | None
    error: str | None


def parse_iso_time(value: object) -> float | None:
    """Parse a gpsd ISO 8601 UTC timestamp (``2026-01-01T00:00:00.000Z``)."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _opt_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


class GpsdReader:
    """Background thread maintaining the latest validated fix from gpsd."""

    def __init__(
        self,
        host: str,
        port: int,
        stale_after: float,
        device: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        time_trusted: Callable[[], bool] = lambda: False,
    ):
        self.host = host
        self.port = port
        self.stale_after = stale_after
        self.device = device
        self._clock = clock
        self._wall = wall_clock
        self._time_trusted = time_trusted
        self._lock = threading.Lock()
        self._fix: Fix | None = None
        self._connected = False
        self._last_report: float | None = None
        self._error: str | None = "starting"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gpsd", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> GpsStatus:
        with self._lock:
            return GpsStatus(self._connected, self._fix, self._last_report, self._error)

    def current_fix(self) -> Fix | None:
        """Return the latest fix if it is still fresh, otherwise None."""
        with self._lock:
            fix = self._fix
        if fix is None or self._clock() - fix.received_monotonic > self.stale_after:
            return None
        return fix

    # -- report handling (also used directly by tests) ----------------------

    def handle_report(self, report: dict) -> None:
        """Process one decoded gpsd JSON object."""
        if report.get("class") != "TPV":
            return
        if self.device and report.get("device") not in (None, self.device):
            return
        now = self._clock()
        with self._lock:
            self._last_report = now
            mode = report.get("mode")
            if not isinstance(mode, int) or mode < 2:
                # Explicit no-fix report: invalidate immediately.
                if self._fix is not None:
                    log.info("gpsd reports no fix")
                self._fix = None
                self._error = "no fix"
                return
            lat, lon = report.get("lat"), report.get("lon")
            if not valid_lat_lon(lat, lon):
                self._fix = None
                self._error = "invalid position in TPV report"
                return
            reported = parse_iso_time(report.get("time"))
            if reported is not None and self._time_trusted():
                age = self._wall() - reported
                if age > self.stale_after:
                    # A replayed or delayed report is not a current position.
                    self._fix = None
                    self._error = f"stale TPV report ({age:.0f} s old)"
                    return
            if self._fix is None:
                log.info("gpsd fix acquired (%dD)", mode)
            self._fix = Fix(
                lat=float(lat), lon=float(lon), mode=mode, received_monotonic=now,
                reported_utc=reported,
                speed_mps=_opt_float(report.get("speed")),
                track_deg=_opt_float(report.get("track")),
                eph_m=_opt_float(report.get("eph")),
                device=report.get("device") if isinstance(report.get("device"), str) else None,
            )
            self._error = None

    # -- socket loop --------------------------------------------------------

    def _watch_command(self) -> bytes:
        watch: dict = {"enable": True, "json": True}
        if self.device:
            watch["device"] = self.device
        return f"?WATCH={json.dumps(watch)};\n".encode()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except OSError as exc:
                with self._lock:
                    if self._connected or self._error != str(exc):
                        log.warning("gpsd connection to %s:%d failed: %s", self.host, self.port, exc)
                    self._connected = False
                    self._fix = None
                    self._error = f"gpsd unavailable: {exc}"
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)

    def _session(self) -> None:
        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            self._sock = sock
            sock.settimeout(max(self.stale_after, 5.0))
            sock.sendall(self._watch_command())
            with self._lock:
                self._connected = True
                self._error = "waiting for fix"
            log.info("connected to gpsd at %s:%d", self.host, self.port)
            buf = b""
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    # Silence: the controller treats the fix as stale; keep waiting.
                    continue
                if not chunk:
                    raise ConnectionError("gpsd closed the connection")
                buf += chunk
                if len(buf) > MAX_LINE_BYTES and b"\n" not in buf:
                    raise ConnectionError("oversized gpsd report")
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        report = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(report, dict):
                        self.handle_report(report)
        self._sock = None
