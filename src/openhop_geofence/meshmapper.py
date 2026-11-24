"""MeshMapper coverage integration.

The worker downloads the authorized region boundary (Zones API) and the
regional coverage dataset (Coverage API), validates them, and keeps a compact
in-memory index for local lookups. Requests are scheduled within the API's
quota and per-IP limits; no request is triggered by GPS fixes.

Storage:
* In-memory dataset and lookup index.
* One gzip JSON snapshot in the cache directory, written only when validated
  content changes, so coverage survives an offline reboot.
* Request accounting and backoff in the RAM-backed runtime directory.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import io
import json
import logging
import math
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import policy, zones
from .config import COVERAGE_TYPES, MeshMapperConfig, read_secret
from .openhop import RadioSettings
from .state import RuntimeDir, atomic_write

log = logging.getLogger(__name__)

SNAPSHOT_FILE = "coverage.json.gz"
ACCOUNTING_FILE = "meshmapper-requests.json"
SNAPSHOT_VERSION = 1

MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_SQUARES = 100_000
MIN_COVERAGE_INTERVAL = 900  # MeshMapper's recommended minimum polling interval
MIN_REQUEST_SPACING = 30  # Seconds between any two requests (per-IP throttle)
HTTP_TIMEOUT = 30

# Lookup bucket size in degrees. Squares are ~0.0027 x 0.00384 degrees.
_BUCKET_DEG = 0.01


class MeshMapperError(Exception):
    """A failed or rejected MeshMapper request."""

    def __init__(self, message: str, *, retry_after: float | None = None, config_fault: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.config_fault = config_fault


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Square:
    south: float
    west: float
    north: float
    east: float
    type: str
    timestamp: float | None  # Unix seconds of the dominant measurement
    snr: float | None = None
    effective: float | None = None
    grid_id: str = ""

    def contains(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east

    def to_compact(self) -> list:
        return [self.south, self.west, self.north, self.east, self.type,
                self.timestamp, self.snr, self.effective, self.grid_id]

    @classmethod
    def from_compact(cls, row: list) -> "Square":
        return cls(*row)


@dataclass
class Dataset:
    """A validated coverage dataset with a bucketed lookup index."""

    squares: list[Square]
    generated_at: float
    fetched_at: float
    filters: dict[str, str]
    key_id: str
    scope: tuple[str, ...]
    boundaries: list[dict]  # GeoJSON geometries of the authorized region(s)
    boundaries_fetched_at: float
    etag: str | None = None
    _index: dict[tuple[int, int], list[Square]] = field(default_factory=dict, repr=False)
    _boundary_shapes: list = field(default_factory=list, repr=False)

    def build_index(self) -> None:
        index: dict[tuple[int, int], list[Square]] = {}
        for sq in self.squares:
            for i in range(math.floor(sq.south / _BUCKET_DEG), math.floor(sq.north / _BUCKET_DEG) + 1):
                for j in range(math.floor(sq.west / _BUCKET_DEG), math.floor(sq.east / _BUCKET_DEG) + 1):
                    index.setdefault((i, j), []).append(sq)
        self._index = index
        self._boundary_shapes = [zones.build_polygon_shape(g) for g in self.boundaries]

    def in_scope(self, lat: float, lon: float) -> bool:
        if not self._boundary_shapes:
            return False
        point = zones.load_shapely().geometry.Point(lon, lat)
        return any(shape.covers(point) for shape in self._boundary_shapes)

    def find(self, lat: float, lon: float) -> Square | None:
        key = (math.floor(lat / _BUCKET_DEG), math.floor(lon / _BUCKET_DEG))
        for sq in self._index.get(key, ()):
            if sq.contains(lat, lon):
                return sq
        return None

    def content_hash(self) -> str:
        payload = json.dumps(
            [[s.to_compact() for s in self.squares], self.boundaries, self.filters, self.scope],
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Parsing helpers (pure; covered by tests)
# ---------------------------------------------------------------------------


def normalize_timestamp(value: Any) -> float | None:
    """Return Unix seconds; values above 2e10 are milliseconds per the API."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value / 1000.0 if value > 2e10 else float(value)


def _format_decimal(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def radio_filter(radio: RadioSettings) -> str:
    """Convert OpenHop radio settings (Hz) to MeshMapper's ``freqMHz,bwKHz,SF,CR``."""
    return ",".join((
        _format_decimal(radio.frequency_hz / 1e6),
        _format_decimal(radio.bandwidth_hz / 1e3),
        str(radio.spreading_factor),
        str(radio.coding_rate),
    ))


def _opt_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def parse_coverage(data: Any) -> tuple[list[Square], float]:
    """Validate a coverage response. Returns (squares, generated_at).

    Raises ValueError for any incompatible content; a partially valid dataset
    is never returned.
    """
    if not isinstance(data, dict) or data.get("success") is not True:
        raise ValueError("coverage response is not successful")
    generated_at = normalize_timestamp(data.get("generated_at"))
    if generated_at is None:
        raise ValueError("coverage response has no valid generated_at")
    rows = data.get("grid_squares")
    if not isinstance(rows, list):
        raise ValueError("coverage response has no grid_squares array")
    if len(rows) > MAX_SQUARES:
        raise ValueError(f"coverage response exceeds {MAX_SQUARES} squares")
    squares = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("grid square is not an object")
        b = row.get("bounds")
        if not isinstance(b, dict):
            raise ValueError("grid square has no bounds")
        try:
            s, w, n, e = (float(b[k]) for k in ("south", "west", "north", "east"))
        except (KeyError, TypeError, ValueError):
            raise ValueError("grid square has invalid bounds") from None
        if not (zones.valid_lat_lon(s, w) and zones.valid_lat_lon(n, e) and s < n and w < e):
            raise ValueError(f"grid square {row.get('grid_id')!r} has invalid bounds")
        ctype = row.get("coverage_type")
        if ctype not in COVERAGE_TYPES:
            raise ValueError(f"unknown coverage_type {ctype!r}")
        squares.append(Square(
            s, w, n, e, ctype, normalize_timestamp(row.get("timestamp")),
            _opt_number(row.get("snr")), _opt_number(row.get("effective")),
            str(row.get("grid_id", ""))[:32],
        ))
    return squares, generated_at


def parse_boundaries(data: Any) -> list[dict]:
    """Return Polygon geometries from a region GeoJSON FeatureCollection.

    A null geometry means unknown scope and makes the whole boundary unusable.
    """
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError("boundary response is not a GeoJSON FeatureCollection")
    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("boundary response has no features")
    geometries = []
    for feature in features:
        geom = feature.get("geometry") if isinstance(feature, dict) else None
        if geom is None:
            code = (feature.get("properties") or {}).get("code") if isinstance(feature, dict) else None
            raise ValueError(f"region {code or '?'} has no boundary (unknown scope)")
        zones.build_polygon_shape(geom)  # Validates; raises ValueError
        geometries.append(geom)
    return geometries


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _bounded_gunzip(raw: bytes) -> bytes:
    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = decomp.decompress(raw, MAX_DECOMPRESSED_BYTES + 1)
    if len(out) > MAX_DECOMPRESSED_BYTES or decomp.unconsumed_tail:
        raise MeshMapperError("decompressed response exceeds the size limit")
    if not decomp.eof:
        raise MeshMapperError("incomplete gzip response")
    return out


def _http_get(url: str, api_key: str, etag: str | None = None) -> tuple[int, bytes, dict[str, str]]:
    """GET with gzip and size limits. Returns (status, body, headers)."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-API-Key", api_key)
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Encoding", "gzip")
    req.add_header("User-Agent", "openhop-geofence")
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as exc:
        resp = exc  # HTTPError is a response object with status and body
    except (urllib.error.URLError, OSError) as exc:
        raise MeshMapperError(f"network error: {getattr(exc, 'reason', exc)}") from None
    with resp:
        status = resp.status if hasattr(resp, "status") else resp.code
        headers = {k.lower(): v for k, v in resp.headers.items()}
        try:
            raw = resp.read(MAX_COMPRESSED_BYTES + 1)
        except (OSError, http.client.IncompleteRead) as exc:
            raise MeshMapperError(f"incomplete response: {exc}") from None
    if len(raw) > MAX_COMPRESSED_BYTES:
        raise MeshMapperError("response exceeds the size limit")
    if headers.get("content-encoding", "").lower() == "gzip" and raw:
        raw = _bounded_gunzip(raw)
    return status, raw, headers


def _error_for_status(status: int, body: bytes, headers: dict[str, str]) -> MeshMapperError:
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        payload = {}
    code = payload.get("error", "") if isinstance(payload, dict) else ""
    retry_after = None
    if headers.get("retry-after", "").isdigit():
        retry_after = float(headers["retry-after"])
    if status == 429 and code == "rate_limit_exceeded":
        # Daily quota: wait for the reset at midnight UTC.
        now = datetime.now(timezone.utc)
        midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp() + 86400
        return MeshMapperError("daily request quota exhausted", retry_after=midnight - now.timestamp() + 60)
    if status == 429:
        return MeshMapperError("per-IP throttle", retry_after=retry_after or 300)
    if status in (400, 401, 403, 404):
        return MeshMapperError(f"HTTP {status} {code}".strip(), config_fault=True)
    return MeshMapperError(f"HTTP {status} {code}".strip(), retry_after=retry_after)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeshMapperStatus:
    enabled: bool
    available: bool
    reason: str
    squares: int = 0
    generated_at: float | None = None
    fetched_at: float | None = None
    requests_today: int = 0
    daily_budget: int = 0
    backoff_until_utc: float | None = None
    last_error: str | None = None
    filters: dict[str, str] = field(default_factory=dict)
    experimental: bool = False


class MeshMapperWorker:
    """Background downloader and local coverage lookup."""

    def __init__(
        self,
        cfg: MeshMapperConfig,
        cache_dir: str | Path,
        runtime: RuntimeDir,
        radio_provider: Callable[[], RadioSettings | None],
        time_trusted: Callable[[], bool],
        wall_clock: Callable[[], float] = time.time,
        http_get: Callable[..., tuple[int, bytes, dict[str, str]]] = _http_get,
    ):
        self.cfg = cfg
        self.cache_path = Path(cache_dir) / SNAPSHOT_FILE
        self.runtime = runtime
        self._radio = radio_provider
        self._trusted = time_trusted
        self._wall = wall_clock
        self._http_get = http_get
        self._lock = threading.Lock()
        self._dataset: Dataset | None = None
        self._saved_hash: str | None = None
        self._last_error: str | None = None
        self._config_fault: str | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._api_key = read_secret(cfg.api_key_file)
        self._next_refresh_jitter = random.uniform(0, 0.1)

    # -- configuration ------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.cfg.enabled and self._api_key and self.cfg.region_urls)

    @property
    def key_id(self) -> str:
        """Non-reversible identifier of the API key (never the key itself)."""
        return hashlib.sha256((self._api_key or "").encode()).hexdigest()[:16]

    def unconfigured_reason(self) -> str | None:
        if not self.cfg.enabled:
            return "disabled"
        if not zones.shapely_available():
            return "Shapely is not installed"
        if not self._api_key:
            return "no API key file"
        if not self.cfg.region_urls:
            return "no region URLs configured"
        return None

    def current_filters(self) -> dict[str, str] | None:
        """Return request filters, or None if the radio is not yet known."""
        filters = {"f_days": str(self.cfg.lookback_days)}
        if self.cfg.radio_filter:
            filters["f_radio_freq"] = self.cfg.radio_filter
        elif self.cfg.match_radio:
            radio = self._radio()
            if radio is None:
                return None
            if radio.multi_radio:
                raise MeshMapperError(
                    "multi-radio OpenHop: set meshmapper.radio_filter explicitly", config_fault=True
                )
            filters["f_radio_freq"] = radio_filter(radio)
        return filters

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._load_snapshot()
        self._thread = threading.Thread(target=self._run, name="meshmapper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=HTTP_TIMEOUT + 5)

    @property
    def alive(self) -> bool:
        return self._thread is None or self._thread.is_alive()

    # -- lookup -------------------------------------------------------------

    def lookup(self, lat: float, lon: float) -> policy.CoverageResult:
        """Classify a position. Never performs network I/O."""
        reason = self.unconfigured_reason()
        if reason:
            return policy.CoverageResult(False, reason=reason)
        if not self._trusted():
            return policy.CoverageResult(False, reason="system time not synchronized")
        with self._lock:
            ds = self._dataset
        if ds is None:
            return policy.CoverageResult(False, reason="no coverage data")
        now = self._wall()
        if now - ds.generated_at > self.cfg.max_cache_age_seconds:
            return policy.CoverageResult(False, reason="coverage data expired")
        if now - ds.boundaries_fetched_at > self.cfg.max_boundary_age_seconds:
            return policy.CoverageResult(False, reason="region boundary expired")
        if not ds.in_scope(lat, lon):
            return policy.CoverageResult(False, reason="outside verified region")
        sq = ds.find(lat, lon)
        cutoff = now - self.cfg.lookback_days * 86400
        if sq is None or (sq.timestamp is not None and sq.timestamp < cutoff):
            return policy.CoverageResult(True, policy.UNCOVERED, "no recent measurement")
        return policy.CoverageResult(True, self.classify(sq), sq.type, sq.grid_id)

    def classify(self, sq: Square) -> str:
        if sq.type not in self.cfg.covered_types:
            return policy.UNCOVERED
        x = self.cfg.experimental
        if x.weak_coverage_thresholds:
            if x.min_snr_db is not None and sq.snr is not None and sq.snr < x.min_snr_db:
                return policy.WEAK
            if x.min_effective is not None and sq.effective is not None and sq.effective < x.min_effective:
                return policy.WEAK
        return policy.COVERED

    def squares_near(self, lat: float, lon: float, radius_deg: float = 0.02) -> list[Square]:
        """Return squares near a position (used by tracing; no network I/O)."""
        with self._lock:
            ds = self._dataset
        if ds is None:
            return []
        out = []
        for i in range(math.floor((lat - radius_deg) / _BUCKET_DEG), math.floor((lat + radius_deg) / _BUCKET_DEG) + 1):
            for j in range(math.floor((lon - radius_deg) / _BUCKET_DEG), math.floor((lon + radius_deg) / _BUCKET_DEG) + 1):
                out.extend(ds._index.get((i, j), ()))
        return list({id(s): s for s in out}.values())

    def coverage_revision(self) -> str | None:
        with self._lock:
            return self._saved_hash[:12] if self._saved_hash else None

    def status(self) -> MeshMapperStatus:
        reason = self.unconfigured_reason() or self._config_fault
        acct = self._accounting()
        with self._lock:
            ds = self._dataset
        available = reason is None and ds is not None and self._trusted()
        if reason is None:
            if ds is None:
                reason = "no coverage data"
            elif not self._trusted():
                reason = "system time not synchronized"
            elif self._wall() - ds.generated_at > self.cfg.max_cache_age_seconds:
                reason, available = "coverage data expired", False
            else:
                reason = "ok"
        return MeshMapperStatus(
            enabled=self.cfg.enabled, available=available, reason=reason,
            squares=len(ds.squares) if ds else 0,
            generated_at=ds.generated_at if ds else None,
            fetched_at=ds.fetched_at if ds else None,
            requests_today=acct.get("count", 0) if acct.get("day") == self._utc_day() else 0,
            daily_budget=self.cfg.daily_request_budget,
            backoff_until_utc=acct.get("backoff_until_utc"),
            last_error=self._last_error,
            filters=dict(ds.filters) if ds else {},
            experimental=self.cfg.experimental.weak_coverage_thresholds,
        )

    # -- snapshot -----------------------------------------------------------

    def _load_snapshot(self) -> None:
        if self.unconfigured_reason():
            return
        try:
            with gzip.open(self.cache_path, "rb") as fh:
                data = json.loads(fh.read(MAX_DECOMPRESSED_BYTES + 1))
        except FileNotFoundError:
            return
        except (OSError, ValueError, EOFError) as exc:
            log.warning("ignoring unreadable coverage snapshot: %s", exc)
            return
        try:
            if data.get("version") != SNAPSHOT_VERSION or data.get("key_id") != self.key_id:
                raise ValueError("key or version changed")
            if tuple(data.get("scope", ())) != tuple(self.cfg.region_urls):
                raise ValueError("region scope changed")
            ds = Dataset(
                squares=[Square.from_compact(r) for r in data["squares"]],
                generated_at=float(data["generated_at"]),
                fetched_at=float(data["fetched_at"]),
                filters=dict(data["filters"]),
                key_id=data["key_id"],
                scope=tuple(data["scope"]),
                boundaries=list(data["boundaries"]),
                boundaries_fetched_at=float(data["boundaries_fetched_at"]),
            )
            ds.build_index()
        except (KeyError, TypeError, ValueError) as exc:
            log.info("coverage snapshot not used: %s", exc)
            return
        with self._lock:
            self._dataset = ds
            self._saved_hash = ds.content_hash()
        log.info("loaded coverage snapshot: %d squares", len(ds.squares))

    def _save_snapshot(self, ds: Dataset) -> None:
        digest = ds.content_hash()
        if digest == self._saved_hash:
            return  # Unchanged content: no persistent write.
        payload = {
            "version": SNAPSHOT_VERSION, "key_id": ds.key_id, "scope": list(ds.scope),
            "filters": ds.filters, "generated_at": ds.generated_at, "fetched_at": ds.fetched_at,
            "boundaries": ds.boundaries, "boundaries_fetched_at": ds.boundaries_fetched_at,
            "squares": [s.to_compact() for s in ds.squares],
        }
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            gz.write(json.dumps(payload, separators=(",", ":")).encode())
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.cache_path, buf.getvalue(), mode=0o640)
        except OSError as exc:
            log.warning("could not write coverage snapshot: %s", exc)
            return
        self._saved_hash = digest
        log.info("saved coverage snapshot: %d squares", len(ds.squares))

    # -- request accounting (RAM-backed) ------------------------------------

    def _utc_day(self) -> str:
        return datetime.fromtimestamp(self._wall(), timezone.utc).strftime("%Y-%m-%d")

    def _accounting(self) -> dict:
        return self.runtime.read_json(ACCOUNTING_FILE) or {}

    def _record_request(self) -> None:
        acct = self._accounting()
        day = self._utc_day()
        if acct.get("day") != day:
            acct = {"day": day, "count": 0}
        acct["count"] = int(acct.get("count", 0)) + 1
        acct["last_request_utc"] = self._wall()
        self.runtime.write_json(ACCOUNTING_FILE, acct)

    def _set_backoff(self, seconds: float) -> None:
        acct = self._accounting()
        acct["backoff_until_utc"] = self._wall() + seconds
        acct["failures"] = int(acct.get("failures", 0)) + 1
        self.runtime.write_json(ACCOUNTING_FILE, acct)

    def _clear_backoff(self) -> None:
        acct = self._accounting()
        if acct.pop("backoff_until_utc", None) is not None or acct.pop("failures", None):
            self.runtime.write_json(ACCOUNTING_FILE, acct)

    def _may_request(self) -> bool:
        acct = self._accounting()
        now = self._wall()
        if acct.get("day") == self._utc_day() and acct.get("count", 0) >= self.cfg.daily_request_budget:
            return False
        if acct.get("backoff_until_utc", 0) > now:
            return False
        return now - acct.get("last_request_utc", 0) >= MIN_REQUEST_SPACING

    # -- refresh loop -------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception:  # Worker must not die silently; log and continue.
                log.exception("unexpected MeshMapper worker error")
                self._last_error = "internal error (see log)"
            self._wake.wait(60)
            self._wake.clear()

    def refresh_once(self) -> None:
        """Perform at most the requests that are due. Called once per minute."""
        if self.unconfigured_reason() or self._config_fault:
            return
        if not self._trusted():
            # TLS validation and quota accounting need a correct clock.
            return
        now = self._wall()
        with self._lock:
            ds = self._dataset
        try:
            filters = self.current_filters()
        except MeshMapperError as exc:
            self._fault(str(exc))
            return
        if filters is None:
            return  # Waiting for OpenHop radio settings.
        if ds is not None and ds.filters != filters:
            log.info("MeshMapper filters changed; discarding cached coverage")
            with self._lock:
                self._dataset = ds = None

        boundaries, boundaries_at = (ds.boundaries, ds.boundaries_fetched_at) if ds else (None, 0.0)
        need_boundary = boundaries is None or now - boundaries_at >= self.cfg.boundary_refresh_seconds
        interval = max(self.cfg.refresh_seconds * (1 + self._next_refresh_jitter), MIN_COVERAGE_INTERVAL)
        need_coverage = ds is None or now - ds.fetched_at >= interval
        if not (need_boundary or need_coverage) or not self._may_request():
            return
        try:
            if need_boundary:
                boundaries = self._fetch_boundaries()
                boundaries_at = self._wall()
            squares, generated_at, etag = (
                self._fetch_coverage(filters, ds) if need_coverage or ds is None else (None, None, None)
            )
        except MeshMapperError as exc:
            self._last_error = str(exc)
            if exc.config_fault:
                self._fault(str(exc))
            else:
                failures = int(self._accounting().get("failures", 0))
                delay = exc.retry_after or min(MIN_COVERAGE_INTERVAL * 2 ** failures, 6 * 3600)
                self._set_backoff(delay)
                log.warning("MeshMapper request failed: %s (retry in %.0f s)", exc, delay)
            return
        self._clear_backoff()
        self._last_error = None
        self._next_refresh_jitter = random.uniform(0, 0.1)
        if squares is None:
            # 304 Not Modified or boundary-only refresh: keep measurements and
            # generated_at; only the fetch time advances.
            assert ds is not None
            new = Dataset(ds.squares, ds.generated_at, self._wall(), ds.filters, ds.key_id,
                          ds.scope, boundaries, boundaries_at, ds.etag)
        else:
            new = Dataset(squares, generated_at, self._wall(), filters, self.key_id,
                          tuple(self.cfg.region_urls), boundaries, boundaries_at, etag)
        try:
            new.build_index()
        except (ValueError, zones.ShapelyUnavailable) as exc:
            self._last_error = f"invalid region boundary: {exc}"
            return
        with self._lock:
            self._dataset = new
        self._save_snapshot(new)

    def _fault(self, message: str) -> None:
        if self._config_fault != message:
            log.error("MeshMapper unavailable until configuration reload: %s", message)
        self._config_fault = message
        self._last_error = message

    def _get_json(self, url: str, etag: str | None = None) -> tuple[Any, dict[str, str], int]:
        self._record_request()
        status, body, headers = self._http_get(url, self._api_key, etag)
        if status == 304:
            return None, headers, status
        if status != 200:
            raise _error_for_status(status, body, headers)
        try:
            return json.loads(body), headers, status
        except ValueError:
            raise MeshMapperError("incomplete or invalid JSON response") from None

    def _fetch_boundaries(self) -> list[dict]:
        geometries: list[dict] = []
        for base in self.cfg.region_urls:
            url = base if base.endswith(".php") else base.rstrip("/") + "/get_geojson.php"
            data, _, _ = self._get_json(url)
            try:
                geometries.extend(parse_boundaries(data))
            except ValueError as exc:
                raise MeshMapperError(f"{url}: {exc}") from None
        return geometries

    def _fetch_coverage(self, filters: dict[str, str], ds: Dataset | None):
        url = self.cfg.coverage_url + "?" + urllib.parse.urlencode(filters)
        etag = None
        # Use a conditional request only while the cached data is young enough
        # that keeping its original generated_at is acceptable.
        if ds is not None and ds.etag and self._wall() - ds.generated_at < self.cfg.max_cache_age_seconds / 2:
            etag = ds.etag
        data, headers, status = self._get_json(url, etag)
        if status == 304:
            return None, None, None
        try:
            squares, generated_at = parse_coverage(data)
        except ValueError as exc:
            raise MeshMapperError(f"invalid coverage response: {exc}") from None
        returned = data.get("filters") if isinstance(data, dict) else None
        if filters.get("f_radio_freq") and not returned:
            raise MeshMapperError("filters were not applied (multi-region key?)", config_fault=True)
        log.info("MeshMapper coverage refreshed: %d squares", len(squares))
        return squares, generated_at, headers.get("etag")
