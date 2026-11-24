"""OpenHop repeater API client.

Contract (inspected at OpenHop commit 13eb8b2; installed releases may differ):

* ``GET /api/stats`` returns ``config.repeater.mode`` and the live radio
  settings under ``config.radio`` (frequency and bandwidth in Hz).
* ``POST /api/set_mode`` with ``{"mode": ...}`` returns
  ``{"success": true, "mode": ..., "persisted": true}``; errors are returned as
  ``{"success": false, "error": ...}``, often with HTTP 200.
* Authentication uses an administrator-equivalent token in ``X-API-Key``.

HTTP 200 alone is never treated as success: every write is confirmed by
reading the mode back.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

MODES = ("forward", "monitor", "no_tx")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class OpenHopError(Exception):
    """Base class for OpenHop client errors."""


class OpenHopUnavailable(OpenHopError):
    """Network failure or timeout. The mode is unknown, not faulty."""


class OpenHopAuthError(OpenHopError):
    """Authentication or authorization failure (HTTP 401/403)."""


class OpenHopProtocolError(OpenHopError):
    """Response did not match the documented API contract."""


@dataclass(frozen=True)
class RadioSettings:
    """Radio parameters used to match MeshMapper filters."""

    frequency_hz: int
    bandwidth_hz: int
    spreading_factor: int
    coding_rate: int
    multi_radio: bool = False


@dataclass(frozen=True)
class OpenHopState:
    mode: str
    radio: RadioSettings | None


class OpenHopClient:
    """Small synchronous client with explicit timeouts."""

    def __init__(self, base_url: str, api_key: str | None, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (401, 403):
                raise OpenHopAuthError(f"HTTP {exc.code} from {path}") from None
            if exc.code >= 500:
                raise OpenHopUnavailable(f"HTTP {exc.code} from {path}") from None
            raise OpenHopProtocolError(f"HTTP {exc.code} from {path}") from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise OpenHopUnavailable(f"{path}: {reason}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise OpenHopProtocolError(f"{path}: response too large")
        try:
            decoded = json.loads(raw)
        except ValueError:
            raise OpenHopProtocolError(f"{path}: response is not JSON") from None
        if not isinstance(decoded, dict):
            raise OpenHopProtocolError(f"{path}: response is not a JSON object")
        return decoded

    def read_state(self) -> OpenHopState:
        """Read the current mode and radio settings."""
        stats = self._request("GET", "/api/stats")
        config = stats.get("config")
        repeater = config.get("repeater") if isinstance(config, dict) else None
        mode = repeater.get("mode") if isinstance(repeater, dict) else None
        if not isinstance(mode, str):
            err = stats.get("error")
            raise OpenHopProtocolError(
                f"/api/stats: missing config.repeater.mode{f' ({err})' if err else ''}"
            )
        if mode not in MODES:
            raise OpenHopProtocolError(f"/api/stats: unknown mode {mode!r}")
        return OpenHopState(mode=mode, radio=_parse_radio(config))

    def set_mode(self, mode: str) -> None:
        """Request a mode. Success requires a valid application response.

        Callers must still read the mode back; a timeout means the outcome is
        unknown.
        """
        if mode not in MODES:
            raise ValueError(f"invalid OpenHop mode {mode!r}")
        resp = self._request("POST", "/api/set_mode", {"mode": mode})
        if resp.get("success") is not True:
            raise OpenHopProtocolError(f"/api/set_mode rejected: {resp.get('error', 'no detail')}")
        if resp.get("mode") != mode:
            raise OpenHopProtocolError(f"/api/set_mode returned mode {resp.get('mode')!r}")

    def list_companions(self) -> list[dict]:
        """Return configured companions (``GET /api/companion/``)."""
        resp = self._request("GET", "/api/companion/")
        data = resp.get("data")
        if resp.get("success") is not True or not isinstance(data, list):
            raise OpenHopProtocolError("/api/companion/: unexpected response")
        return [c for c in data if isinstance(c, dict)]


def _parse_radio(config: dict) -> RadioSettings | None:
    radio = config.get("radio")
    radios = config.get("radios")
    multi = isinstance(radios, list) and len(radios) > 1
    if not isinstance(radio, dict):
        return None
    try:
        values = [radio[k] for k in ("frequency", "bandwidth", "spreading_factor", "coding_rate")]
    except KeyError:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return None
    freq, bw, sf, cr = values
    return RadioSettings(int(round(freq)), int(round(bw)), int(sf), int(cr), multi)
