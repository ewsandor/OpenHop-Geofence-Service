"""Configuration loading and validation.

The configuration file is TOML (``/etc/openhop-geofence/config.toml`` by
default). It is parsed with the standard-library ``tomllib`` and validated
strictly: unknown keys, wrong types, and out-of-range values are errors.
Secrets (API keys, web passwords, BLE PINs) are stored in separate files and
referenced by path.
"""

from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import math
import re
import tomllib
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import zones as zones_mod

DEFAULT_CONFIG_PATH = "/etc/openhop-geofence/config.toml"
DEFAULT_STATE_DIR = "/var/lib/openhop-geofence"
DEFAULT_CACHE_DIR = "/var/cache/openhop-geofence"
DEFAULT_RUNTIME_DIR = "/run/openhop-geofence"

COVERAGE_TYPES = ("BIDIR", "TX", "RX", "DISC", "DEAD", "DROP")
REPORT_LEVELS = ("info", "warning", "error")
_HEX_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_RADIO_FILTER_RE = re.compile(r"^\d+(\.\d+)?,\d+(\.\d+)?,\d{1,2},\d$")


class ConfigError(ValueError):
    """Raised for an invalid configuration. The message lists every problem."""


# ---------------------------------------------------------------------------
# Section dataclasses. Field defaults are the documented defaults.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenHopConfig:
    base_url: str = "http://127.0.0.1:8000"
    api_key_file: str = "/etc/openhop-geofence/secrets/openhop.key"
    timeout_seconds: float = 2.0


@dataclass(frozen=True)
class GpsdConfig:
    host: str = "127.0.0.1"
    port: int = 2947
    stale_after_seconds: float = 10.0
    device: str | None = None


@dataclass(frozen=True)
class PolicyConfig:
    monitor_mode: str = "monitor"
    outside: str = "monitor"
    repeat_delay_seconds: float = 10.0
    minimum_monitor_seconds: float = 30.0


@dataclass(frozen=True)
class MeshMapperConfig:
    enabled: bool = False
    api_key_file: str = "/etc/openhop-geofence/secrets/meshmapper.key"
    coverage_url: str = "https://meshmapper.net/coverage.php"
    country: str = ""
    regions: list[str] = field(default_factory=list)
    lookback_days: int = 14
    covered_types: list[str] = field(default_factory=lambda: ["BIDIR", "TX", "RX", "DISC"])
    match_radio: bool = True
    radio_filter: str | None = None
    refresh_seconds: int = 21600
    max_cache_age_seconds: int = 86400
    boundary_refresh_seconds: int = 86400
    max_boundary_age_seconds: int = 604800
    daily_request_budget: int = 80
    weak_coverage_thresholds: bool = False
    min_snr_db: float | None = None
    min_effective: float | None = None


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8080
    auth_file: str = "/etc/openhop-geofence/secrets/web-auth"
    allow_config_edit: bool = True
    map_enabled: bool = True
    leaflet_dir: str = "/usr/share/javascript/leaflet"
    tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    tile_attribution: str = (
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    )


@dataclass(frozen=True)
class CompanionConfig:
    enabled: bool = False
    transport: str = "tcp"
    host: str = "127.0.0.1"
    port: int = 5000
    openhop_companion_name: str | None = None
    shares_openhop_radio: bool = True
    report_channels: list[int] = field(default_factory=lambda: [0])
    report_level: str = "warning"
    outage_warning_after_seconds: float = 300.0
    minimum_report_interval_seconds: float = 60.0
    command_prefix: str = "!geofence"
    command_authorization: str = "admin_keys"
    admin_public_keys: list[str] = field(default_factory=list)
    announce_failure_enabled: bool = True
    announce_failure_max_attempts: int = 3
    announce_failure_grace_seconds: float = 10.0
    serial_device: str | None = None
    serial_baud: int = 115200
    ble_address: str | None = None
    ble_pin_file: str | None = None


@dataclass(frozen=True)
class MaintenanceConfig:
    allow_reboot: bool = True


@dataclass(frozen=True)
class DiagnosticsConfig:
    enabled: bool = False
    storage: str = "ram"
    sample_interval_seconds: float = 5.0
    max_buffer_bytes: int = 4 * 1024 * 1024
    max_context_bytes: int = 8 * 1024 * 1024
    directory: str | None = None
    max_disk_bytes: int | None = None
    max_age_seconds: int | None = None


@dataclass(frozen=True)
class Config:
    """A complete, validated configuration."""

    openhop: OpenHopConfig = field(default_factory=OpenHopConfig)
    gpsd: GpsdConfig = field(default_factory=GpsdConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    meshmapper: MeshMapperConfig = field(default_factory=MeshMapperConfig)
    web: WebConfig = field(default_factory=WebConfig)
    companion: CompanionConfig = field(default_factory=CompanionConfig)
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)
    zones: tuple[zones_mod.Zone, ...] = ()
    #: Short SHA-256 of the file contents; identifies the configuration revision.
    revision: str = ""
    #: Non-fatal notices shown by ``--check-config`` and the web interface.
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Generic table -> dataclass conversion
# ---------------------------------------------------------------------------


def _type_ok(value: Any, tp: Any) -> tuple[bool, Any]:
    """Check ``value`` against a simple annotated type; return (ok, coerced)."""
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return _type_ok(value, args[0])
    if origin is list:
        (item_tp,) = typing.get_args(tp)
        if not isinstance(value, list):
            return False, value
        out = []
        for item in value:
            ok, coerced = _type_ok(item, item_tp)
            if not ok:
                return False, value
            out.append(coerced)
        return True, out
    if tp is bool:
        return isinstance(value, bool), value
    if tp is int:
        return isinstance(value, int) and not isinstance(value, bool), value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, value
        return math.isfinite(value), float(value)
    if tp is str:
        return isinstance(value, str), value
    return False, value


def _type_name(tp: Any) -> str:
    origin = typing.get_origin(tp)
    if origin is list:
        return f"list of {_type_name(typing.get_args(tp)[0])}"
    args = [a for a in typing.get_args(tp) if a is not type(None)]
    if args and origin is not list:
        return _type_name(args[0])
    return getattr(tp, "__name__", str(tp))


def _load_section(cls: type, table: Any, prefix: str, errors: list[str]):
    """Build dataclass ``cls`` from a TOML table, recording errors."""
    if not isinstance(table, dict):
        errors.append(f"[{prefix}] must be a table")
        return cls()
    hints = typing.get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    for key in table:
        if key not in known:
            errors.append(f"{prefix}.{key}: unknown setting")
    values: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in table:
            continue
        tp = hints[f.name]
        raw = table[f.name]
        if dataclasses.is_dataclass(tp):
            values[f.name] = _load_section(tp, raw, f"{prefix}.{f.name}", errors)
            continue
        ok, coerced = _type_ok(raw, tp)
        if not ok:
            errors.append(f"{prefix}.{f.name}: expected {_type_name(tp)}")
            continue
        values[f.name] = coerced
    return cls(**values)


# ---------------------------------------------------------------------------
# Section validators
# ---------------------------------------------------------------------------


def _check_range(errors: list[str], name: str, value: float | int | None, lo: float, hi: float) -> None:
    if value is not None and not lo <= value <= hi:
        errors.append(f"{name}: must be between {lo:g} and {hi:g}")


def _check_url(errors: list[str], name: str, url: str, schemes: tuple[str, ...]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in schemes or not parsed.netloc:
        errors.append(f"{name}: must be a {' or '.join(schemes)} URL")


def _validate(cfg: Config, errors: list[str], warnings: list[str]) -> None:
    o = cfg.openhop
    _check_url(errors, "openhop.base_url", o.base_url, ("http", "https"))
    _check_range(errors, "openhop.timeout_seconds", o.timeout_seconds, 0.1, 30)

    g = cfg.gpsd
    _check_range(errors, "gpsd.port", g.port, 1, 65535)
    _check_range(errors, "gpsd.stale_after_seconds", g.stale_after_seconds, 1, 300)

    p = cfg.policy
    if p.monitor_mode not in ("monitor", "no_tx"):
        errors.append('policy.monitor_mode: must be "monitor" or "no_tx"')
    if p.outside not in ("monitor", "repeat", "last"):
        errors.append('policy.outside: must be "monitor", "repeat", or "last"')
    _check_range(errors, "policy.repeat_delay_seconds", p.repeat_delay_seconds, 0, 3600)
    _check_range(errors, "policy.minimum_monitor_seconds", p.minimum_monitor_seconds, 0, 86400)

    m = cfg.meshmapper
    _check_url(errors, "meshmapper.coverage_url", m.coverage_url, ("https",))
    if m.country and not re.fullmatch(r"[A-Z]{2}", m.country):
        errors.append("meshmapper.country: expected an uppercase two-letter country code")
    if m.regions and not m.country:
        errors.append("meshmapper.country: required when regions are configured")
    if any(not re.fullmatch(r"[A-Z0-9]{3}", region) for region in m.regions):
        errors.append("meshmapper.regions: expected uppercase three-character region codes")
    if len(set(m.regions)) != len(m.regions):
        errors.append("meshmapper.regions: duplicate region codes")
    _check_range(errors, "meshmapper.lookback_days", m.lookback_days, 1, 365)
    for t in m.covered_types:
        if t not in COVERAGE_TYPES:
            errors.append(f"meshmapper.covered_types: unknown type {t!r}")
    if m.radio_filter is not None and not _RADIO_FILTER_RE.match(m.radio_filter):
        errors.append('meshmapper.radio_filter: expected "freqMHz,bwKHz,SF,CR", e.g. "906.875,250,10,5"')
    if m.refresh_seconds < 900:
        errors.append("meshmapper.refresh_seconds: must be at least 900 (MeshMapper minimum interval)")
    if m.max_cache_age_seconds < m.refresh_seconds:
        errors.append("meshmapper.max_cache_age_seconds: must be at least refresh_seconds")
    if m.boundary_refresh_seconds < 3600:
        errors.append("meshmapper.boundary_refresh_seconds: must be at least 3600")
    if m.max_boundary_age_seconds < m.boundary_refresh_seconds:
        errors.append("meshmapper.max_boundary_age_seconds: must be at least boundary_refresh_seconds")
    _check_range(errors, "meshmapper.daily_request_budget", m.daily_request_budget, 1, 1000)
    _check_range(errors, "meshmapper.min_effective", m.min_effective, 0, 3)
    _check_range(errors, "meshmapper.min_snr_db", m.min_snr_db, -40, 40)
    if m.weak_coverage_thresholds:
        if m.min_snr_db is None and m.min_effective is None:
            errors.append(
                "meshmapper.weak_coverage_thresholds: set min_snr_db and/or min_effective"
            )
        else:
            warnings.append("MeshMapper weak-coverage thresholds are experimental")
    if m.enabled and not zones_mod.shapely_available():
        warnings.append("MeshMapper requires Shapely (python3-shapely); it will be unavailable")

    w = cfg.web
    _check_range(errors, "web.port", w.port, 1, 65535)
    try:
        ipaddress.ip_address(w.bind)
    except ValueError:
        errors.append("web.bind: must be an IP address")
    if "{z}" not in w.tile_url or not w.tile_url.startswith("https://"):
        errors.append("web.tile_url: must be an https URL template containing {z}, {x}, and {y}")

    c = cfg.companion
    if c.transport not in ("tcp", "ble", "serial"):
        errors.append('companion.transport: must be "tcp", "ble", or "serial"')
    if c.enabled and c.transport == "serial" and not c.serial_device:
        errors.append("companion.serial_device: required for the serial transport")
    if c.enabled and c.transport == "ble" and not c.ble_address:
        errors.append("companion.ble_address: required for the BLE transport")
    _check_range(errors, "companion.port", c.port, 1, 65535)
    for ch in c.report_channels:
        _check_range(errors, "companion.report_channels", ch, 0, 255)
    if c.report_level not in REPORT_LEVELS:
        errors.append('companion.report_level: must be "info", "warning", or "error"')
    if c.command_authorization not in ("admin_keys", "any_sender"):
        errors.append('companion.command_authorization: must be "admin_keys" or "any_sender"')
    if c.enabled and c.command_authorization == "any_sender":
        warnings.append("Companion control is permitted from ANY direct-message sender (any_sender)")
    for key in c.admin_public_keys:
        if not _HEX_KEY_RE.match(key):
            errors.append("companion.admin_public_keys: each key must be 64 hexadecimal characters")
            break
    if not c.command_prefix.strip() or len(c.command_prefix) > 32:
        errors.append("companion.command_prefix: must be 1-32 characters")
    _check_range(errors, "companion.announce_failure_max_attempts", c.announce_failure_max_attempts, 0, 3)
    _check_range(errors, "companion.announce_failure_grace_seconds", c.announce_failure_grace_seconds, 0, 60)
    _check_range(errors, "companion.outage_warning_after_seconds", c.outage_warning_after_seconds, 0, 86400)
    _check_range(errors, "companion.minimum_report_interval_seconds", c.minimum_report_interval_seconds, 0, 86400)

    d = cfg.diagnostics
    if d.storage not in ("ram", "disk"):
        errors.append('diagnostics.storage: must be "ram" or "disk"')
    _check_range(errors, "diagnostics.sample_interval_seconds", d.sample_interval_seconds, 1, 3600)
    _check_range(errors, "diagnostics.max_buffer_bytes", d.max_buffer_bytes, 65536, 256 * 1024 * 1024)
    _check_range(errors, "diagnostics.max_context_bytes", d.max_context_bytes, 65536, 256 * 1024 * 1024)
    if d.storage == "disk":
        if not d.directory or not d.max_disk_bytes or not d.max_age_seconds:
            errors.append(
                "diagnostics: disk storage requires directory, max_disk_bytes, and max_age_seconds"
            )
        elif d.max_disk_bytes <= 0 or d.max_age_seconds <= 0:
            errors.append("diagnostics: max_disk_bytes and max_age_seconds must be positive")
        elif not Path(d.directory).is_absolute():
            errors.append("diagnostics.directory: must be an absolute path")


def _load_zones(raw: Any, errors: list[str]) -> tuple[zones_mod.Zone, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        errors.append("zones: must be an array of tables ([[zones]])")
        return ()
    out: list[zones_mod.Zone] = []
    for i, z in enumerate(raw):
        name = f"zones[{i}]"
        try:
            out.append(parse_zone(z))
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
            continue
    return tuple(out)


_CIRCLE_KEYS = {"name", "role", "shape", "latitude", "longitude", "radius_m"}
_POLYGON_KEYS = {"name", "role", "shape", "geometry"}


def parse_zone(z: Any) -> zones_mod.Zone:
    """Validate one zone table and return a Zone. Raises ValueError."""
    if not isinstance(z, dict):
        raise ValueError("must be a table")
    zname, role, shape = z.get("name"), z.get("role"), z.get("shape", "circle")
    if not isinstance(zname, str) or not zname.strip() or len(zname) > 100:
        raise ValueError("name: must be 1-100 characters")
    if role not in (zones_mod.ROLE_REPEAT, zones_mod.ROLE_MONITOR):
        raise ValueError('role: must be "repeat" or "monitor"')
    if shape == "circle":
        unknown = set(z) - _CIRCLE_KEYS
        if unknown:
            raise ValueError(f"unknown setting(s) {sorted(unknown)}")
        lat, lon, radius = z.get("latitude"), z.get("longitude"), z.get("radius_m")
        if not zones_mod.valid_lat_lon(lat, lon):
            raise ValueError("latitude/longitude: required, finite and in range")
        if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 0 < radius <= 100_000:
            raise ValueError("radius_m: must be greater than 0 and at most 100000")
        return zones_mod.CircleZone(zname, role, float(lat), float(lon), float(radius))
    if shape == "geojson":
        unknown = set(z) - _POLYGON_KEYS
        if unknown:
            raise ValueError(f"unknown setting(s) {sorted(unknown)}")
        geometry = z.get("geometry")
        try:
            prepared = zones_mod.build_polygon_shape(_plain(geometry))
        except zones_mod.ShapelyUnavailable as exc:
            raise ValueError(f"polygon zones need Shapely: {exc}") from exc
        return zones_mod.PolygonZone(zname, role, _plain(geometry), prepared)
    raise ValueError('shape: must be "circle" or "geojson"')


def _plain(value: Any) -> Any:
    """Convert tomlkit/TOML containers into plain dicts and lists."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


_SECTIONS = {
    "openhop": OpenHopConfig,
    "gpsd": GpsdConfig,
    "policy": PolicyConfig,
    "meshmapper": MeshMapperConfig,
    "web": WebConfig,
    "companion": CompanionConfig,
    "maintenance": MaintenanceConfig,
    "diagnostics": DiagnosticsConfig,
}


def config_revision(text: str) -> str:
    """Short SHA-256 of configuration text, used to detect concurrent edits."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def parse_config(text: str) -> Config:
    """Parse and validate configuration text. Raises ConfigError."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML syntax error: {exc}") from exc
    errors: list[str] = []
    warnings: list[str] = []
    sections: dict[str, Any] = {}
    for key in data:
        if key not in _SECTIONS and key != "zones":
            errors.append(f"[{key}]: unknown section")
    for name, cls in _SECTIONS.items():
        sections[name] = _load_section(cls, data.get(name, {}), name, errors)
    zones = _load_zones(data.get("zones"), errors)
    revision = config_revision(text)
    cfg = Config(**sections, zones=zones, revision=revision)
    _validate(cfg, errors, warnings)
    if errors:
        raise ConfigError("invalid configuration:\n  " + "\n  ".join(errors))
    return dataclasses.replace(cfg, warnings=tuple(warnings))


def load_config(path: str | Path) -> Config:
    """Read and validate a configuration file. Raises ConfigError or OSError."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_config(text)


def read_secret(path: str | None) -> str | None:
    """Return the stripped contents of a secret file, or None if missing/empty."""
    if not path:
        return None
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        return None
    return value or None


#: Settings only an administrator may change through the web interface.
ADMIN_ONLY_SETTINGS = (
    ("openhop", "base_url"),
    ("openhop", "api_key_file"),
    ("meshmapper", "api_key_file"),
    ("meshmapper", "coverage_url"),
    ("meshmapper", "country"),
    ("meshmapper", "regions"),
    ("web",),
    ("companion", "command_authorization"),
    ("companion", "admin_public_keys"),
    ("companion", "ble_pin_file"),
    ("companion", "transport"),
    ("companion", "serial_device"),
    ("maintenance",),
    ("diagnostics", "storage"),
    ("diagnostics", "directory"),
    ("diagnostics", "max_disk_bytes"),
    ("diagnostics", "max_age_seconds"),
)


def changed_settings(old: Config, new: Config, paths: tuple[tuple[str, ...], ...]) -> list[str]:
    """Return dotted names in ``paths`` whose values differ between two configs."""
    changed = []
    for path in paths:
        a: Any = old
        b: Any = new
        for part in path:
            a = getattr(a, part)
            b = getattr(b, part)
        if a != b:
            changed.append(".".join(path))
    return changed


def restart_required(old: Config, new: Config) -> list[str]:
    """Settings that take effect only after a service restart."""
    paths = (("web",), ("companion",), ("diagnostics",), ("gpsd",), ("maintenance",))
    return changed_settings(old, new, paths)
