"""Mode policy: decision priority and hysteresis.

This module is pure logic with no I/O so it can be tested exhaustively.

Decision priority (first match wins):

1. Fault safe mode: ``no_tx``.
2. Explicit operator TX lockout: ``no_tx``.
3. Manual Force Repeat or Force Monitor override.
4. Missing or stale GPS: Monitor.
5. Any containing Monitor zone: Monitor.
6. Any containing Repeat zone: Repeat.
7. MeshMapper, when enabled and valid for the position: covered squares
   select Monitor; uncovered or weakly covered squares select Repeat.
8. Outside policy (``policy.outside``): Monitor, Repeat, or Last.

The Monitor policy result is applied as OpenHop ``monitor`` or ``no_tx``
according to ``policy.monitor_mode``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .zones import ZoneMatch

REPEAT = "repeat"
MONITOR = "monitor"

# Coverage classifications.
COVERED = "covered"
UNCOVERED = "uncovered"
WEAK = "weak"

# Decision sources, in priority order.
SRC_SAFE_MODE = "safe_mode"
SRC_TX_LOCKOUT = "tx_lockout"
SRC_OVERRIDE = "override"
SRC_GPS = "gps"
SRC_MONITOR_ZONE = "monitor_zone"
SRC_REPEAT_ZONE = "repeat_zone"
SRC_MESHMAPPER = "meshmapper"
SRC_OUTSIDE = "outside"

#: Sources whose confirmed result updates the mode held by the Last policy.
HELD_SOURCES = (SRC_MONITOR_ZONE, SRC_REPEAT_ZONE, SRC_MESHMAPPER)


@dataclass(frozen=True)
class CoverageResult:
    """MeshMapper classification for the current position."""

    available: bool
    classification: str | None = None  # COVERED, UNCOVERED, or WEAK
    reason: str = ""
    grid_id: str | None = None


@dataclass(frozen=True)
class Decision:
    """Outcome of the decision priority before hysteresis."""

    source: str
    #: Policy result: ``repeat`` or ``monitor`` for automatic decisions; for
    #: manual and safe mode decisions the OpenHop mode itself.
    result: str
    #: Requested OpenHop mode before hysteresis.
    openhop_mode: str
    reason: str
    #: Automatic decisions are subject to hysteresis; overrides and safe mode are not.
    automatic: bool


def map_result(result: str, monitor_mode: str) -> str:
    """Map a Repeat/Monitor policy result to an OpenHop mode."""
    return "forward" if result == REPEAT else monitor_mode


def held_from_mode(openhop_mode: str | None) -> str | None:
    """Convert an observed OpenHop mode to the policy result held by Last."""
    if openhop_mode is None:
        return None
    return REPEAT if openhop_mode == "forward" else MONITOR


def decide(
    *,
    safe_mode: bool,
    override: str | None,
    fix_valid: bool,
    zone_match: ZoneMatch | None,
    coverage: CoverageResult | None,
    outside: str,
    held: str | None,
    monitor_mode: str,
    tx_lockout: bool = False,
) -> Decision:
    """Apply the decision priority.

    ``override`` is ``"forward"`` (Force Repeat), ``"monitor"`` (Force
    Monitor), or None. ``held`` is the result held by the Last policy.
    """
    if safe_mode:
        return Decision(SRC_SAFE_MODE, "no_tx", "no_tx", "safe mode", False)
    if tx_lockout:
        return Decision(SRC_TX_LOCKOUT, "no_tx", "no_tx", "operator TX lockout", False)
    if override == "forward":
        return Decision(SRC_OVERRIDE, "forward", "forward", "Force Repeat override", False)
    if override == "monitor":
        return Decision(SRC_OVERRIDE, "monitor", "monitor", "Force Monitor override", False)

    def auto(source: str, result: str, reason: str) -> Decision:
        return Decision(source, result, map_result(result, monitor_mode), reason, True)

    if not fix_valid:
        return auto(SRC_GPS, MONITOR, "no valid GPS fix")
    if zone_match and zone_match.monitor:
        return auto(SRC_MONITOR_ZONE, MONITOR, "in Monitor zone " + ", ".join(zone_match.monitor))
    if zone_match and zone_match.repeat:
        return auto(SRC_REPEAT_ZONE, REPEAT, "in Repeat zone " + ", ".join(zone_match.repeat))
    if coverage is not None and coverage.available:
        if coverage.classification == COVERED:
            return auto(SRC_MESHMAPPER, MONITOR, "MeshMapper square covered")
        if coverage.classification == WEAK:
            return auto(SRC_MESHMAPPER, REPEAT, "MeshMapper square weakly covered (experimental)")
        return auto(SRC_MESHMAPPER, REPEAT, "MeshMapper square uncovered")
    suffix = f" (MeshMapper unavailable: {coverage.reason})" if coverage is not None else ""
    if outside == "repeat":
        return auto(SRC_OUTSIDE, REPEAT, "outside zones: Repeat policy" + suffix)
    if outside == "last":
        held_result = held or MONITOR
        why = "held" if held else "no mode known"
        return auto(SRC_OUTSIDE, held_result, f"outside zones: Last policy ({why}){suffix}")
    return auto(SRC_OUTSIDE, MONITOR, "outside zones: Monitor policy" + suffix)


class Hysteresis:
    """Repeat dwell and minimum Monitor time, using monotonic seconds.

    Transitions to Monitor are immediate. Repeat requires continuous
    eligibility for ``repeat_delay`` and at least ``minimum_monitor`` since
    entering Monitor. Overrides and either persistent lock bypass the timers.
    """

    def __init__(self, repeat_delay: float, minimum_monitor: float, now: float):
        self.repeat_delay = repeat_delay
        self.minimum_monitor = minimum_monitor
        self.reset(now)

    def reset(self, now: float) -> None:
        """Restart eligibility: behave as if Monitor was entered at ``now``."""
        self.repeating = False
        self.monitor_since = now
        self.candidate_since: float | None = None

    def apply(self, decision: Decision, now: float, monitor_mode: str) -> tuple[str, float | None]:
        """Return (OpenHop mode to apply, seconds until Repeat or None).

        While Repeat eligibility is pending, the Monitor mapping
        (``monitor_mode``) is applied.
        """
        if not decision.automatic:
            # Overrides and persistent locks bypass timers. Track state so a
            # later automatic Monitor decision is handled consistently.
            if decision.openhop_mode == "forward":
                self.repeating = True
            elif self.repeating:
                self.repeating = False
                self.monitor_since = now
            self.candidate_since = None
            return decision.openhop_mode, None

        if decision.result != REPEAT:
            if self.repeating:
                self.repeating = False
                self.monitor_since = now
            self.candidate_since = None
            return decision.openhop_mode, None

        if self.repeating:
            return "forward", None
        if self.candidate_since is None:
            self.candidate_since = now
        wait = max(
            self.repeat_delay - (now - self.candidate_since),
            self.minimum_monitor - (now - self.monitor_since),
        )
        if wait <= 0:
            self.repeating = True
            self.candidate_since = None
            return "forward", None
        return monitor_mode, wait
