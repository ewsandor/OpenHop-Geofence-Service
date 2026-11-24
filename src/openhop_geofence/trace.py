"""Optional diagnostic location and mode trace (DESIGN.md section 9).

Records are versioned JSON Lines kept in a bounded RAM ring buffer. Zone
definitions and MeshMapper squares used for decisions are kept once each in
a separate bounded context store. Disk recording is an explicit local opt-in
using a bounded writer queue, batched writes, and size/age-limited rotation.

Tracing never makes network requests and never blocks the controller: hooks
only enqueue work, and all failures are counted and reported as diagnostics.
"""

from __future__ import annotations

import collections
import io
import json
import logging
import os
import queue
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import DiagnosticsConfig, Config

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
WRITER_QUEUE_MAX = 1024
WRITER_FLUSH_SECONDS = 30.0
ROTATE_PARTS = 8  # Disk budget is split across this many files for pruning


def _dumps(obj: Any) -> bytes:
    return (json.dumps(obj, separators=(",", ":"), default=str) + "\n").encode()


class TraceRecorder:
    """Bounded recorder fed by controller evaluation hooks and events."""

    def __init__(
        self,
        cfg: DiagnosticsConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        time_trusted: Callable[[], bool] = lambda: False,
        coverage_lookup: Callable[[float, float], list] | None = None,
        coverage_revision: Callable[[], str | None] | None = None,
        spool_dir: str | None = None,
    ):
        self.cfg = cfg
        self.clock = clock
        self.wall = wall_clock
        self.time_trusted = time_trusted
        self.coverage_lookup = coverage_lookup
        self.coverage_revision = coverage_revision or (lambda: None)
        self.spool_dir = spool_dir
        self.session = uuid.uuid4().hex[:12]
        self.recording = cfg.enabled
        self._start = clock()
        self._lock = threading.Lock()
        self._records: collections.deque[bytes] = collections.deque()
        self._record_bytes = 0
        self._context: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self._context_bytes = 0
        self.dropped_records = 0
        self.dropped_context = 0
        self.error: str | None = None
        self._last_sample = -1e18
        self._last_key: tuple | None = None
        self._config_revision: str | None = None
        # Disk writer
        self._queue: queue.Queue[bytes | None] | None = None
        self._writer: threading.Thread | None = None
        if cfg.enabled and cfg.storage == "disk":
            self._queue = queue.Queue(maxsize=WRITER_QUEUE_MAX)
            self._writer = threading.Thread(target=self._write_loop, name="trace-writer", daemon=True)
            self._writer.start()

    # -- controller hooks -------------------------------------------------------

    def set_config(self, cfg: Config) -> None:
        """Record zone context for a configuration revision (once per revision)."""
        if not self.cfg.enabled or cfg.revision == self._config_revision:
            return
        self._config_revision = cfg.revision
        features = [z.to_geojson() for z in cfg.zones]
        self._add_context(f"zones:{cfg.revision}", {
            "type": "zones", "config_revision": cfg.revision,
            "geojson": {"type": "FeatureCollection", "features": features},
        })

    def on_evaluation(self, ev: Any) -> None:
        """Controller hook: sample positions and record transitions immediately."""
        if not (self.cfg.enabled and self.recording):
            return
        now = self.clock()
        d = ev.decision
        key = (ev.fix is None, ev.observed_mode, ev.desired_mode, d.source, ev.request_error)
        changed = key != self._last_key
        due = ev.fix is not None and now - self._last_sample >= self.cfg.sample_interval_seconds
        if not (changed or due):
            return
        self._last_key = key
        if ev.fix is not None:
            self._last_sample = now
        cov_rev = self.coverage_revision()
        record = {
            "v": SCHEMA_VERSION,
            "type": "sample" if not changed else "transition",
            "session": self.session,
            "utc": self.wall() if self.time_trusted() else None,
            "elapsed": round(now - self._start, 1),
            "fix": ev.fix is not None,
            "lat": round(ev.fix.lat, 6) if ev.fix else None,
            "lon": round(ev.fix.lon, 6) if ev.fix else None,
            "fix_age": round(now - ev.fix.received_monotonic, 1) if ev.fix else None,
            "fix_mode": ev.fix.mode if ev.fix else None,
            "result": d.result,
            "source": d.source,
            "reason": d.reason,
            "desired": ev.desired_mode,
            # Confirmed mode only; a stale observation is recorded as None.
            "confirmed": ev.observed_mode if ev.observed_age is not None else None,
            "confirmed_age": ev.observed_age,
            "request": ev.request_mode,
            "request_error": ev.request_error,
            "zones": list(ev.zone_ids),
            "coverage": (
                {"class": ev.coverage.classification, "grid": ev.coverage.grid_id,
                 "available": ev.coverage.available, "reason": ev.coverage.reason}
                if ev.coverage else None
            ),
            "config_revision": self._config_revision,
            "coverage_revision": cov_rev,
        }
        self._append(record)
        if ev.fix is not None and ev.coverage and ev.coverage.available and self.coverage_lookup and cov_rev:
            for sq in self.coverage_lookup(ev.fix.lat, ev.fix.lon):
                self._add_context(f"sq:{cov_rev}:{sq.grid_id}", {
                    "type": "square", "coverage_revision": cov_rev, "grid_id": sq.grid_id,
                    "bounds": [sq.south, sq.west, sq.north, sq.east], "coverage_type": sq.type,
                    "timestamp": sq.timestamp, "snr": sq.snr, "effective": sq.effective,
                })

    def on_event(self, event: Any) -> None:
        """Controller listener: record notable events without location."""
        if not (self.cfg.enabled and self.recording):
            return
        self._append({
            "v": SCHEMA_VERSION, "type": "event", "session": self.session,
            "utc": event.time_utc, "elapsed": round(self.clock() - self._start, 1),
            "level": event.level, "kind": event.kind, "message": event.message,
        })

    # -- storage ------------------------------------------------------------

    def _append(self, record: dict) -> None:
        line = _dumps(record)
        with self._lock:
            self._records.append(line)
            self._record_bytes += len(line)
            while self._record_bytes > self.cfg.max_buffer_bytes and self._records:
                self._record_bytes -= len(self._records.popleft())
                if self._queue is None:
                    self.dropped_records += 1
        self._enqueue(line)

    def _add_context(self, key: str, item: dict) -> None:
        with self._lock:
            if key in self._context:
                self._context.move_to_end(key)  # Recently referenced context is kept.
                return
            data = _dumps(item)
            if len(data) > self.cfg.max_context_bytes:
                self.dropped_context += 1
                return
            self._context[key] = data
            self._context_bytes += len(data)
            while self._context_bytes > self.cfg.max_context_bytes:
                _, old = self._context.popitem(last=False)
                self._context_bytes -= len(old)
                self.dropped_context += 1
        self._enqueue(data, context=True)

    def _enqueue(self, line: bytes, context: bool = False) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait((b"C" if context else b"R") + line)
        except queue.Full:
            self.dropped_records += 1
            self.error = "disk writer queue overflow"

    def _write_loop(self) -> None:
        """Batch queued lines to rotating files; never fsync per sample."""
        directory = Path(self.cfg.directory or "")
        part_limit = max(64 * 1024, (self.cfg.max_disk_bytes or 0) // ROTATE_PARTS)
        batch: dict[str, list[bytes]] = {"records": [], "context": []}
        last_flush = time.monotonic()
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            self._prune(directory)
        except OSError as exc:
            self.error = f"trace directory: {exc}"
        while True:
            try:
                item = self._queue.get(timeout=5)
            except queue.Empty:
                item = b""
            if item is None:
                break
            if item:
                batch["context" if item[:1] == b"C" else "records"].append(item[1:])
            if time.monotonic() - last_flush >= WRITER_FLUSH_SECONDS or sum(map(len, batch.values())) >= 256:
                self._flush(directory, batch, part_limit)
                last_flush = time.monotonic()
        self._flush(directory, batch, part_limit)

    def _flush(self, directory: Path, batch: dict[str, list[bytes]], part_limit: int) -> None:
        for kind, lines in batch.items():
            if not lines:
                continue
            try:
                current = self._current_file(directory, kind, part_limit)
                with open(current, "ab") as fh:
                    fh.write(b"".join(lines))
                os.chmod(current, 0o640)
                self._prune(directory)
                self.error = None if self.error and "directory" not in self.error else self.error
            except OSError as exc:
                self.dropped_records += len(lines)
                self.error = f"trace write failed: {exc}"
            lines.clear()

    def _current_file(self, directory: Path, kind: str, part_limit: int) -> Path:
        files = sorted(directory.glob(f"{kind}-{self.session}-*.jsonl"))
        if files and files[-1].stat().st_size < part_limit:
            return files[-1]
        return directory / f"{kind}-{self.session}-{len(files):04d}.jsonl"

    def _prune(self, directory: Path) -> None:
        """Delete the oldest files beyond the total size or age limit."""
        files = sorted(
            (p for p in directory.glob("*.jsonl") if p.is_file()), key=lambda p: p.stat().st_mtime
        )
        now = time.time()
        total = sum(p.stat().st_size for p in files)
        for p in files:
            too_old = self.cfg.max_age_seconds and now - p.stat().st_mtime > self.cfg.max_age_seconds
            if too_old or total > (self.cfg.max_disk_bytes or 0):
                total -= p.stat().st_size
                p.unlink(missing_ok=True)

    def stop(self) -> None:
        if self._queue is not None:
            try:
                self._queue.put(None, timeout=1)
            except queue.Full:
                pass
            if self._writer:
                self._writer.join(timeout=10)

    # -- interface actions ----------------------------------------------------

    def set_recording(self, enabled: bool) -> None:
        if not self.cfg.enabled:
            raise RuntimeError("diagnostics are disabled in the configuration")
        self.recording = enabled
        log.info("trace recording %s", "started" if enabled else "stopped")

    def clear(self) -> None:
        """Clear RAM records and disk files (the context is retained)."""
        with self._lock:
            self._records.clear()
            self._record_bytes = 0
        if self.cfg.storage == "disk" and self.cfg.directory:
            for p in Path(self.cfg.directory).glob("records-*.jsonl"):
                p.unlink(missing_ok=True)
        self._last_key = None

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.cfg.enabled,
                "recording": self.recording and self.cfg.enabled,
                "storage": self.cfg.storage,
                "records": len(self._records),
                "buffer_bytes": self._record_bytes,
                "max_buffer_bytes": self.cfg.max_buffer_bytes,
                "context_bytes": self._context_bytes,
                "max_context_bytes": self.cfg.max_context_bytes,
                "max_disk_bytes": self.cfg.max_disk_bytes,
                "max_age_seconds": self.cfg.max_age_seconds,
                "dropped_records": self.dropped_records,
                "dropped_context": self.dropped_context,
                "error": self.error,
            }

    def snapshot(self) -> tuple[list[dict], list[dict]]:
        """Return RAM (records, context) as decoded objects."""
        with self._lock:
            lines = list(self._records)
            ctx = list(self._context.values())
        return [json.loads(x) for x in lines], [json.loads(x) for x in ctx]

    def export_zip(self) -> io.BufferedRandom | tempfile.SpooledTemporaryFile:
        """Build a bounded ZIP archive; spools to the runtime directory when large."""
        out = tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024, dir=self.spool_dir)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            if self.cfg.storage == "disk" and self.cfg.directory:
                directory = Path(self.cfg.directory)
                for kind in ("records", "context"):
                    with zf.open(f"{kind}.jsonl", "w") as dst:
                        for p in sorted(directory.glob(f"{kind}-*.jsonl"), key=lambda p: p.stat().st_mtime):
                            with open(p, "rb") as src:
                                while chunk := src.read(65536):
                                    dst.write(chunk)
            else:
                with self._lock:
                    records = b"".join(self._records)
                    context = b"".join(self._context.values())
                zf.writestr("records.jsonl", records)
                zf.writestr("context.jsonl", context)
            zf.writestr("manifest.json", json.dumps({
                "format": "openhop-geofence-trace", "schema_version": SCHEMA_VERSION,
                "service_version": __version__, "session": self.session,
                "exported_utc": self.wall() if self.time_trusted() else None,
                "dropped_records": self.dropped_records, "dropped_context": self.dropped_context,
                "files": {"records.jsonl": "trace records (JSON Lines)",
                          "context.jsonl": "zone and MeshMapper square context (JSON Lines)"},
            }, indent=2))
        out.seek(0)
        return out
