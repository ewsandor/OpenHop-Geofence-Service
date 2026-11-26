"""The controller: the single mode authority.

One thread (normally the main thread) runs :meth:`Controller.run`, which
evaluates the policy once per second, applies hysteresis, and reconciles the
OpenHop mode with read-before-write and readback. Web and companion
interfaces never touch OpenHop directly; they submit commands with
:meth:`Controller.submit` and wait for the returned future.

Safety invariants (see DESIGN.md section 5):

* Fault safe mode and the persistent operator TX lockout require ``no_tx``.
  Both are persisted before the mode is requested.
* Ordinary overrides are refused while either lock is active.
* A requested mode is never reported as confirmed until readback succeeds.
* Unexpected external mode changes are a control-conflict fault.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from . import __version__, config as config_mod, policy, zones
from .config import Config, ConfigError
from .gpsd import Fix, GpsdReader
from .openhop import (
    OpenHopAuthError,
    OpenHopClient,
    OpenHopError,
    OpenHopProtocolError,
    RadioSettings,
)
from .state import (
    SafeModeLatch, SafeModeStore, TxLockout, TxLockoutStore,
    atomic_write, managed_secret_path,
)
from .system import Notifier, RESTART_EXIT_STATUS, request_reboot

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0
OPENHOP_POLL_SECONDS = 5.0
OPENHOP_MAX_BACKOFF = 30.0
API_FAULT_THRESHOLD = 3  # Consecutive auth/protocol errors before a fault
MAX_CONFIG_BYTES = 256 * 1024

INFO, WARNING, ERROR = "info", "warning", "error"


class CommandError(Exception):
    """A rejected controller command. The message is shown to the requester."""


@dataclass(frozen=True)
class Event:
    """A notable state change, delivered to listeners (companion, trace)."""

    level: str  # info, warning, error
    kind: str
    message: str
    time_utc: float | None


class Listener(Protocol):
    def on_event(self, event: Event) -> None: ...


class FailureAnnouncementSender(Protocol):
    def announce_failure_ready(self) -> bool: ...
    def send_announce_failure(self, text: str) -> None: ...


@dataclass(frozen=True)
class Evaluation:
    """Per-tick result passed to the trace recorder (never persisted here)."""

    fix: Fix | None
    decision: policy.Decision
    desired_mode: str
    observed_mode: str | None
    observed_age: float | None
    request_mode: str | None
    request_error: str | None
    zone_ids: tuple[str, ...]
    coverage: policy.CoverageResult | None


class Controller:
    """Evaluates policy and owns all OpenHop mode changes."""

    def __init__(
        self,
        cfg: Config,
        config_path: str | Path,
        *,
        gps: GpsdReader,
        safe_store: SafeModeStore,
        tx_lockout_store: TxLockoutStore,
        make_openhop: Callable[[Config], OpenHopClient],
        make_meshmapper: Callable[[Config], Any] | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        time_trusted: Callable[[], bool] = lambda: False,
        reboot: Callable[[], tuple[bool, str]] = request_reboot,
    ):
        self.cfg = cfg
        self.config_path = Path(config_path)
        self.gps = gps
        self.safe_store = safe_store
        self.tx_lockout_store = tx_lockout_store
        self._make_openhop = make_openhop
        self._make_meshmapper = make_meshmapper
        self.notifier = notifier or Notifier()
        self.clock = clock
        self.wall = wall_clock
        self.time_trusted = time_trusted
        self._reboot = reboot

        self.openhop = make_openhop(cfg)
        self.meshmapper = make_meshmapper(cfg) if make_meshmapper else None
        self.listeners: list[Listener] = []
        self.evaluation_hooks: list[Callable[[Evaluation], None]] = []
        self.announce_failure: FailureAnnouncementSender | None = None
        self.status_providers: dict[str, Callable[[], Any]] = {}

        self._commands: queue.Queue = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self._started_monotonic = clock()

        # Safe mode
        self.latch = safe_store.load()
        self.latch_persisted = True
        self.persistence_error: str | None = None
        self.fault: str | None = None
        self.grace_until: float | None = None
        if self.latch.active:
            log.warning("safe mode latch active at startup: %s", self.latch.cause)
        self.tx_lockout = tx_lockout_store.load()
        self.tx_lockout_persisted = True
        self.tx_lockout_persistence_error: str | None = None
        if self.tx_lockout.active:
            log.warning("operator TX lockout active at startup: %s", self.tx_lockout.cause)

        # Control state
        self.override: str | None = None
        self.held: str | None = None
        self.hysteresis = policy.Hysteresis(
            cfg.policy.repeat_delay_seconds, cfg.policy.minimum_monitor_seconds, clock()
        )
        self.decision: policy.Decision | None = None
        self.desired_mode: str | None = None
        self.repeat_wait: float | None = None
        self.zone_match: zones.ZoneMatch | None = None
        self.coverage: policy.CoverageResult | None = None

        # OpenHop
        self.observed_mode: str | None = None
        self.observed_at: float | None = None
        self.radio: RadioSettings | None = None
        self.openhop_error: str | None = None
        self.request_mode: str | None = None
        self.request_error: str | None = None
        self._connected = False
        self._api_failures = 0
        self._next_poll = 0.0
        self._backoff = 1.0
        self._uncertain_write: str | None = None

        # Configuration and maintenance
        self.config_error: str | None = None
        self.pending_restart: list[str] = []
        self.maintenance: str | None = None
        self.exit_status = 0

        # Outage tracking for warnings
        self._gps_lost_since: float | None = clock()
        self._gps_warned = False
        self._mm_lost_since: float | None = None
        self._mm_warned = False

        self._status: dict[str, Any] = {}
        self._status_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface (thread-safe)
    # ------------------------------------------------------------------

    def submit(self, action: str, **kwargs: Any) -> concurrent.futures.Future:
        """Queue a command for the controller thread.

        Actions: ``override`` (mode), ``resume``, ``disable_tx``, ``clear``,
        ``reload``, ``save_config`` (text, revision, role), ``restart``,
        ``reboot``. All accept ``requester`` for logging. The future resolves
        to a result message or raises :class:`CommandError`.
        """
        fut: concurrent.futures.Future = concurrent.futures.Future()
        try:
            self._commands.put_nowait((action, kwargs, fut))
        except queue.Full:
            fut.set_exception(CommandError("controller busy; try again"))
        return fut

    def call(self, action: str, timeout: float = 20.0, **kwargs: Any) -> str:
        """Submit a command and wait for its result."""
        try:
            return self.submit(action, **kwargs).result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise CommandError("timed out waiting for the controller") from None

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        """Return a JSON-serializable status snapshot (secrets excluded)."""
        with self._status_lock:
            snap = dict(self._status)
        if self.meshmapper is not None:
            mm = self.meshmapper.status()
            snap["meshmapper"] = {
                **{k: getattr(mm, k) for k in mm.__dataclass_fields__},
                "result": snap.get("coverage"),
            }
        for name, provider in self.status_providers.items():
            try:
                snap[name] = provider()
            except Exception as exc:  # A broken provider must not break status.
                snap[name] = {"error": str(exc)}
        return snap

    @property
    def tx_permitted(self) -> bool:
        """True when the confirmed OpenHop mode and policy both permit TX."""
        return (
            not self.latch.active
            and not self.tx_lockout.active
            and self.observed_mode in ("forward", "monitor")
            and self.desired_mode in ("forward", "monitor")
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Run until stopped. Returns the process exit status."""
        if self.meshmapper is not None:
            self.meshmapper.start()
        self.step()
        self.notifier.ready()
        self._emit(INFO, "service", f"service started (version {__version__})")
        next_tick = self.clock() + TICK_SECONDS
        while not self._stop.is_set():
            timeout = max(0.0, next_tick - self.clock())
            try:
                item = self._commands.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is not None:
                self._run_command(*item)
                if self.clock() < next_tick:
                    continue
            next_tick = self.clock() + TICK_SECONDS
            self.step()
        self._shutdown()
        return self.exit_status

    def step(self) -> None:
        """One evaluation. Sends the watchdog notification only on success."""
        try:
            self._evaluate()
        except Exception as exc:
            log.exception("controller evaluation failed")
            self._on_fault(f"internal error: {exc.__class__.__name__}: {exc}", announce_failure=True)
            # Enforce safe mode even when evaluation keeps failing.
            try:
                self._enforce_minimal()
            except Exception:
                log.exception("could not enforce safe mode")
            return
        self.notifier.watchdog()

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def _evaluate(self) -> None:
        now = self.clock()
        cfg = self.cfg
        self._check_grace(now)

        fix = self.gps.current_fix()
        match = zones.match_zones(cfg.zones, fix.lat, fix.lon) if fix else None
        coverage = None
        if fix and self.meshmapper is not None and cfg.meshmapper.enabled:
            coverage = self.meshmapper.lookup(fix.lat, fix.lon)
        self.zone_match, self.coverage = match, coverage

        safe = self.latch.active and self.grace_until is None
        decision = policy.decide(
            safe_mode=safe,
            tx_lockout=self.tx_lockout.active,
            override=self.override,
            fix_valid=fix is not None,
            zone_match=match,
            coverage=coverage,
            outside=cfg.policy.outside,
            held=self.held,
            monitor_mode=cfg.policy.monitor_mode,
        )
        mode, wait = self.hysteresis.apply(decision, now, cfg.policy.monitor_mode)
        changed = decision.reason != (self.decision.reason if self.decision else None)
        self.decision, self.desired_mode, self.repeat_wait = decision, mode, wait
        if changed:
            log.info("policy: %s -> %s (%s)", decision.source, decision.openhop_mode, decision.reason)

        if self.grace_until is None:
            self._sync_openhop(now)
        self._update_held(decision)
        self._track_outages(now, fix, coverage)
        self._publish(now, fix)

        ev = Evaluation(
            fix, decision, mode, self.observed_mode, self._observed_age(now),
            self.request_mode, self.request_error,
            match.names if match else (), coverage,
        )
        for hook in self.evaluation_hooks:
            try:
                hook(ev)
            except Exception:  # Diagnostics must never interrupt control.
                log.exception("evaluation hook failed")

    def _update_held(self, decision: policy.Decision) -> None:
        """Track the result held by the Last policy from confirmed decisions."""
        if self.observed_mode != self.desired_mode or self.observed_mode is None:
            return
        if decision.source in policy.HELD_SOURCES or decision.source == policy.SRC_OVERRIDE:
            # Only confirmed zone, MeshMapper, and override decisions update the
            # held result; GPS loss and pending Repeat do not.
            if decision.source == policy.SRC_OVERRIDE or self.repeat_wait is None:
                self.held = policy.held_from_mode(self.observed_mode)

    # ------------------------------------------------------------------
    # OpenHop reconciliation
    # ------------------------------------------------------------------

    def _observed_age(self, now: float) -> float | None:
        return None if self.observed_at is None else round(now - self.observed_at, 1)

    def _sync_openhop(self, now: float) -> None:
        mismatch = self._connected and self.observed_mode != self.desired_mode
        if not mismatch and now < self._next_poll:
            return
        if not self._read_openhop(now):
            return
        if self.observed_mode != self.desired_mode and self.desired_mode is not None:
            self._write_openhop(now, self.desired_mode)

    def _read_openhop(self, now: float) -> bool:
        try:
            state = self.openhop.read_state()
        except OpenHopError as exc:
            self._openhop_failed(now, exc)
            return False
        previous, was_connected = self.observed_mode, self._connected
        if was_connected and state.mode != previous and state.mode != self._uncertain_write:
            # Something else changed the mode while we were in control.
            self.observed_mode, self.observed_at = state.mode, now
            self._on_fault(
                f"control conflict: OpenHop mode changed externally from {previous} to {state.mode}",
                announce_failure=True,
            )
            return False
        if not was_connected:
            if previous is None:
                # First read at startup: initial held result for Last policy.
                self.held = policy.held_from_mode(state.mode)
                log.info("OpenHop connected; current mode %s", state.mode)
            else:
                log.info("OpenHop reconnected; current mode %s", state.mode)
                self._emit(INFO, "openhop", f"OpenHop reachable again (mode {state.mode})")
            # After (re)connection, apply Monitor before re-enabling Repeat.
            self.hysteresis.reset(now)
        self._connected = True
        self._uncertain_write = None
        self._api_failures = 0
        self._backoff = 1.0
        self.openhop_error = None
        self.observed_mode, self.observed_at = state.mode, now
        self.radio = state.radio
        self._next_poll = now + OPENHOP_POLL_SECONDS
        return True

    def _write_openhop(self, now: float, mode: str) -> None:
        self.request_mode, self.request_error = mode, None
        self._uncertain_write = mode
        try:
            self.openhop.set_mode(mode)
        except OpenHopError as exc:
            self.request_error = str(exc)
            log.error("OpenHop mode request %s failed: %s", mode, exc)
            self._emit(ERROR, "request_failed", f"mode request {mode} failed: {exc}")
            self._openhop_failed(now, exc)
            return
        # Readback is the only confirmation.
        if self._read_openhop(now) and self.observed_mode == mode:
            self.request_mode = None
            log.info("OpenHop mode confirmed: %s", mode)
            self._emit(INFO if mode != "no_tx" else ERROR, "mode", f"OpenHop mode confirmed: {mode}")
        elif self.observed_mode != mode:
            self.request_error = f"readback shows {self.observed_mode}"

    def _openhop_failed(self, now: float, exc: OpenHopError) -> None:
        if self._connected:
            self._emit(WARNING, "openhop", f"OpenHop unavailable: {exc}")
        self._connected = False
        self.openhop_error = str(exc)
        self.observed_at = None  # Observed mode is now unknown/stale.
        self._next_poll = now + self._backoff
        self._backoff = min(self._backoff * 2, OPENHOP_MAX_BACKOFF)
        if isinstance(exc, (OpenHopAuthError, OpenHopProtocolError)):
            self._api_failures += 1
            if self._api_failures == API_FAULT_THRESHOLD:
                self._on_fault(f"OpenHop API fault: {exc}", announce_failure=True)

    def _enforce_minimal(self) -> None:
        """Best-effort ``no_tx`` when the regular evaluation cannot run."""
        if ((self.latch.active or self.tx_lockout.active) and self.grace_until is None
                and self.observed_mode != "no_tx"):
            self.desired_mode = "no_tx"
            self.openhop.set_mode("no_tx")
            self._read_openhop(self.clock())

    # ------------------------------------------------------------------
    # Safe mode
    # ------------------------------------------------------------------

    def _set_latch(self, cause: str) -> bool:
        """Persist an active latch. Returns False if persistence failed."""
        self.latch = SafeModeLatch(True, cause, self.wall() if self.time_trusted() else None)
        try:
            self.safe_store.save(self.latch)
        except OSError as exc:
            self.latch_persisted = False
            self.persistence_error = f"could not persist safe mode latch: {exc}"
            log.error("%s", self.persistence_error)
            self._emit(ERROR, "persistence", self.persistence_error)
            return False
        self.latch_persisted, self.persistence_error = True, None
        return True

    def _on_fault(self, cause: str, *, announce_failure: bool) -> None:
        """Latch safe mode for a service fault, optionally after a failure announcement."""
        if self.latch.active and self.fault == cause:
            return  # Already handled; avoid repeated latching and reports.
        already_active = self.latch.active
        self.fault = cause
        log.error("fault: %s; entering safe mode", cause)
        self.override = None
        persisted = self._set_latch(cause)
        self._emit(ERROR, "fault", f"fault: {cause}; safe mode, requesting no_tx")
        grace = self.cfg.companion.announce_failure_grace_seconds
        if (
            announce_failure and persisted and not already_active and grace > 0
            and self.announce_failure is not None
            and self.observed_mode in ("forward", "monitor")
            and self.announce_failure.announce_failure_ready()
        ):
            self.grace_until = self.clock() + grace
            try:
                self.announce_failure.send_announce_failure(f"FAULT: {cause[:80]}. Entering safe mode (no_tx).")
            except Exception:
                log.exception("failure announcement submission failed")
            log.warning("failure announcement grace period: no_tx within %.0f s", grace)

    def _check_grace(self, now: float) -> None:
        if self.grace_until is not None and now >= self.grace_until:
            log.info("failure announcement grace period ended; requesting no_tx")
            self.grace_until = None

    # ------------------------------------------------------------------
    # Outage warnings
    # ------------------------------------------------------------------

    def _track_outages(self, now: float, fix: Fix | None, coverage: policy.CoverageResult | None) -> None:
        delay = self.cfg.companion.outage_warning_after_seconds
        if fix is None:
            if self._gps_lost_since is None:
                self._gps_lost_since = now
                log.warning("GPS fix lost; applying Monitor")
            elif not self._gps_warned and now - self._gps_lost_since >= delay:
                self._gps_warned = True
                self._emit(WARNING, "gps", f"no valid GPS fix for {int(now - self._gps_lost_since)} s; Monitor applied")
        else:
            if self._gps_warned:
                self._emit(INFO, "gps", "GPS fix recovered")
            elif self._gps_lost_since is not None:
                log.info("GPS fix acquired")
            self._gps_lost_since, self._gps_warned = None, False

        if coverage is None:
            return  # MeshMapper disabled or no fix (GPS warning covers it).
        if not coverage.available:
            if self._mm_lost_since is None:
                self._mm_lost_since = now
            elif not self._mm_warned and now - self._mm_lost_since >= delay:
                self._mm_warned = True
                self._emit(
                    WARNING, "meshmapper",
                    f"MeshMapper coverage unavailable ({coverage.reason}); "
                    f"outside policy {self.cfg.policy.outside} applies",
                )
        else:
            if self._mm_warned:
                self._emit(INFO, "meshmapper", "MeshMapper coverage available again")
            self._mm_lost_since, self._mm_warned = None, False

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _run_command(self, action: str, kwargs: dict, fut: concurrent.futures.Future) -> None:
        if not fut.set_running_or_notify_cancel():
            return
        requester = kwargs.pop("requester", "unknown")
        handler = getattr(self, f"_cmd_{action}", None)
        try:
            if handler is None:
                raise CommandError(f"unknown action {action!r}")
            result = handler(**kwargs)
        except CommandError as exc:
            log.warning("command %s from %s rejected: %s", action, requester, exc)
            fut.set_exception(exc)
            return
        except Exception as exc:
            log.exception("command %s from %s failed", action, requester)
            fut.set_exception(CommandError(f"internal error: {exc}"))
            return
        log.info("command %s from %s: %s", action, requester, result)
        # Evaluate right away so the result is applied without waiting a tick,
        # and resolve afterwards so callers that refresh see the new status.
        try:
            if action in (
                "override", "resume", "disable_tx", "clear", "clear_tx_lockout",
                "reload", "save_config",
            ):
                self.step()
        finally:
            fut.set_result(result)

    def _cmd_override(self, mode: str) -> str:
        if mode not in ("forward", "monitor"):
            raise CommandError("override must be forward or monitor")
        if self.latch.active:
            raise CommandError("safe mode is active; clear safe mode first")
        if self.tx_lockout.active:
            raise CommandError("operator TX lockout is active; clear the TX lockout first")
        self.override = mode
        name = "Force Repeat" if mode == "forward" else "Force Monitor"
        self._emit(INFO, "override", f"{name} override set")
        return f"{name} set; requesting {mode}"

    def _cmd_resume(self) -> str:
        if self.latch.active:
            raise CommandError("safe mode is active; resume cannot clear it")
        if self.tx_lockout.active:
            raise CommandError("operator TX lockout is active; clear the TX lockout first")
        self.override = None
        self.hysteresis.reset(self.clock())
        self._emit(INFO, "override", "automatic control resumed")
        return "automatic control resumed"

    def _cmd_disable_tx(self) -> str:
        self.grace_until = None  # Explicit Disable all TX cancels any grace.
        self.override = None
        lockout = TxLockout(
            True, "Disable all TX requested", self.wall() if self.time_trusted() else None
        )
        self.tx_lockout = lockout
        try:
            self.tx_lockout_store.save(lockout)
            self.tx_lockout_persisted, self.tx_lockout_persistence_error = True, None
        except OSError as exc:
            self.tx_lockout_persisted = False
            self.tx_lockout_persistence_error = f"could not persist operator TX lockout: {exc}"
            log.error("%s", self.tx_lockout_persistence_error)
            self._emit(ERROR, "tx_lockout", self.tx_lockout_persistence_error)
        self._emit(WARNING, "tx_lockout", "Disable all TX: operator TX lockout latched, requesting no_tx")
        if not self.tx_lockout_persisted:
            return f"no_tx requested; WARNING: {self.tx_lockout_persistence_error}"
        return "operator TX lockout latched; no_tx requested (confirmation pending readback)"

    def _cmd_clear_tx_lockout(self) -> str:
        if not self.tx_lockout.active:
            return "operator TX lockout is not active"
        try:
            self.tx_lockout_store.save(TxLockout())
        except OSError as exc:
            raise CommandError(f"cannot clear TX lockout: state not writable: {exc}") from None
        self.tx_lockout = TxLockout()
        self.tx_lockout_persisted, self.tx_lockout_persistence_error = True, None
        self.hysteresis.reset(self.clock())
        if self.latch.active:
            result = "operator TX lockout cleared; fault safe mode remains active"
        else:
            result = "operator TX lockout cleared; automatic control resumed"
        self._emit(INFO, "tx_lockout", result)
        return result

    def _cmd_clear(self) -> str:
        if not self.latch.active:
            return "safe mode is not active"
        problems = []
        try:
            disk = config_mod.load_config(self.config_path)
            if disk.revision != self.cfg.revision:
                problems.append("configuration file changed; reload it first")
        except (OSError, ConfigError) as exc:
            problems.append(f"configuration invalid: {str(exc).splitlines()[0]}")
        if self.config_error:
            problems.append("active configuration has an error; reload a valid file")
        try:
            state = self.openhop.read_state()
            self.observed_mode, self.observed_at = state.mode, self.clock()
            self._connected = True
        except OpenHopError as exc:
            problems.append(f"OpenHop check failed: {exc}")
        if self.gps.current_fix() is None:
            problems.append("no fresh GPS fix")
        if problems:
            raise CommandError("cannot clear safe mode: " + "; ".join(problems))
        cleared = SafeModeLatch()
        try:
            self.safe_store.save(cleared)  # Also verifies durable-state writability.
        except OSError as exc:
            raise CommandError(f"cannot clear safe mode: state not writable: {exc}") from None
        self.latch, self.latch_persisted, self.persistence_error = cleared, True, None
        self.fault = None
        self.override = None
        self._api_failures = 0
        self.hysteresis.reset(self.clock())
        result = (
            "safe mode cleared; operator TX lockout remains active"
            if self.tx_lockout.active
            else "safe mode cleared; automatic control resumed"
        )
        self._emit(INFO, "safe_mode", result)
        return result

    def _cmd_reload(self) -> str:
        try:
            new = config_mod.load_config(self.config_path)
        except (OSError, ConfigError) as exc:
            self.config_error = str(exc)
            self._on_fault(f"invalid configuration on reload: {str(exc).splitlines()[0]}", announce_failure=True)
            raise CommandError(f"reload failed; safe mode latched: {exc}") from None
        return self._apply_config(new)

    def _cmd_save_config(self, text: str, revision: str, role: str) -> str:
        if len(text.encode()) > MAX_CONFIG_BYTES:
            raise CommandError("configuration too large")
        try:
            current_text = self.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"cannot read configuration: {exc}") from None
        current = config_mod.config_revision(current_text)
        if current != revision:
            raise CommandError("conflict: configuration changed since it was loaded")
        try:
            new = config_mod.parse_config(text)
        except ConfigError as exc:
            raise CommandError(str(exc)) from None
        if role != "admin":
            admin = config_mod.changed_settings(self.cfg, new, config_mod.ADMIN_ONLY_SETTINGS)
            if admin:
                raise CommandError(f"administrator role required to change: {', '.join(admin)}")
        try:
            mode = os.stat(self.config_path).st_mode & 0o777
            atomic_write(self.config_path, text.encode("utf-8"), mode=mode)
        except OSError as exc:
            self._emit(ERROR, "persistence", f"configuration save failed: {exc}")
            raise CommandError(f"configuration not saved: {exc}") from None
        return "saved; " + self._apply_config(new)

    def _cmd_update_secret(self, kind: str, value: str) -> str:
        paths = {
            "openhop": self.cfg.openhop.api_key_file,
            "meshmapper": self.cfg.meshmapper.api_key_file,
            "companion_ble_pin": self.cfg.companion.ble_pin_file,
        }
        if kind not in paths:
            raise CommandError("unknown secret")
        if (not isinstance(value, str) or not value or len(value) > 4096
                or value != value.strip() or "\n" in value or "\r" in value or "\0" in value):
            raise CommandError("secret must be a non-empty single line without surrounding whitespace")
        try:
            path = managed_secret_path(self.config_path, paths[kind])
            other_paths = [self.cfg.web.auth_file, *(v for k, v in paths.items() if k != kind and v)]
            if any(path.resolve() == Path(other).resolve() for other in other_paths):
                raise ValueError("secret file path is also used by another credential")
            atomic_write(path, (value + "\n").encode("utf-8"), mode=0o640)
        except (OSError, ValueError) as exc:
            raise CommandError(f"cannot save secret: {exc}") from None
        if kind == "openhop":
            self.openhop = self._make_openhop(self.cfg)
            self._connected = False
            self._next_poll = 0.0
        elif kind == "meshmapper" and self._make_meshmapper is not None:
            if self.meshmapper is not None:
                self.meshmapper.stop()
            self.meshmapper = self._make_meshmapper(self.cfg)
            if self.meshmapper is not None:
                self.meshmapper.start()
        else:
            return "secret saved; restart the service to apply it"
        return "secret saved and applied; clear safe mode after health checks if needed"

    def _apply_config(self, new: Config) -> str:
        """Apply a validated configuration and restart eligibility timers."""
        old = self.cfg
        self.pending_restart = sorted(set(self.pending_restart) | set(config_mod.restart_required(old, new)))
        if new.openhop != old.openhop:
            self.openhop = self._make_openhop(new)
            self._connected = False
            self._next_poll = 0.0
        if new.meshmapper != old.meshmapper and self._make_meshmapper is not None:
            if self.meshmapper is not None:
                self.meshmapper.stop()
            self.meshmapper = self._make_meshmapper(new)
            if self.meshmapper is not None:
                self.meshmapper.start()
        self.cfg = new
        self.config_error = None
        self.hysteresis = policy.Hysteresis(
            new.policy.repeat_delay_seconds, new.policy.minimum_monitor_seconds, self.clock()
        )
        self._emit(INFO, "config", f"configuration {new.revision} applied")
        if self.pending_restart:
            return "applied; restart required for: " + ", ".join(self.pending_restart)
        return f"configuration {new.revision} applied"

    def _cmd_restart(self) -> str:
        if self.maintenance:
            raise CommandError(f"{self.maintenance} already pending")
        self.maintenance = "restart"
        self.exit_status = RESTART_EXIT_STATUS
        self._emit(INFO, "maintenance", "service restart requested")
        self._stop.set()
        return "service restart scheduled"

    def _cmd_reboot(self) -> str:
        if not self.cfg.maintenance.allow_reboot:
            raise CommandError("host reboot is disabled (maintenance.allow_reboot = false)")
        if self.maintenance:
            raise CommandError(f"{self.maintenance} already pending")
        ok, detail = self._reboot()
        if not ok:
            raise CommandError(f"reboot request failed: {detail}")
        self.maintenance = "reboot"
        self._emit(INFO, "maintenance", "host reboot requested")
        return "host reboot scheduled"

    # ------------------------------------------------------------------
    # Status and events
    # ------------------------------------------------------------------

    def _emit(self, level: str, kind: str, message: str) -> None:
        event = Event(level, kind, message, self.wall() if self.time_trusted() else None)
        for listener in self.listeners:
            try:
                listener.on_event(event)
            except Exception:
                log.exception("event listener failed")

    def _publish(self, now: float, fix: Fix | None) -> None:
        d = self.decision
        gps = self.gps.status()
        state = (
            "safe_mode" if self.latch.active
            else "tx_disabled" if self.tx_lockout.active
            else "override" if self.override
            else "automatic"
        )
        snap = {
            "version": __version__,
            "uptime_seconds": int(now - self._started_monotonic),
            "time_trusted": self.time_trusted(),
            "control": {
                "state": state,
                "source": d.source if d else None,
                "reason": d.reason if d else None,
                "policy_result": d.result if d else None,
                "desired_mode": self.desired_mode,
                "repeat_in_seconds": round(self.repeat_wait, 1) if self.repeat_wait else None,
                "override": self.override,
                "outside_policy": self.cfg.policy.outside,
                "monitor_mode": self.cfg.policy.monitor_mode,
            },
            "openhop": {
                "connected": self._connected,
                "observed_mode": self.observed_mode if self.observed_at is not None else None,
                "last_known_mode": self.observed_mode,
                "observed_age_seconds": self._observed_age(now),
                "request_mode": self.request_mode,
                "request_error": self.request_error,
                "error": self.openhop_error,
            },
            "safe_mode": {
                "active": self.latch.active,
                "cause": self.latch.cause,
                "since_utc": self.latch.since_utc,
                "persisted": self.latch_persisted,
                "persistence_error": self.persistence_error,
                "announce_failure_seconds": round(self.grace_until - now, 1) if self.grace_until else None,
            },
            "tx_lockout": {
                "active": self.tx_lockout.active,
                "cause": self.tx_lockout.cause,
                "since_utc": self.tx_lockout.since_utc,
                "persisted": self.tx_lockout_persisted,
                "persistence_error": self.tx_lockout_persistence_error,
            },
            "fault": self.fault,
            "gps": {
                "connected": gps.connected,
                "fix": fix is not None,
                "latitude": round(fix.lat, 6) if fix else None,
                "longitude": round(fix.lon, 6) if fix else None,
                "fix_mode": fix.mode if fix else None,
                "age_seconds": round(now - fix.received_monotonic, 1) if fix else None,
                "error": gps.error,
            },
            "zones": {
                "count": len(self.cfg.zones),
                "monitor": list(self.zone_match.monitor) if self.zone_match else [],
                "repeat": list(self.zone_match.repeat) if self.zone_match else [],
            },
            "coverage": (
                {"available": self.coverage.available, "classification": self.coverage.classification,
                 "reason": self.coverage.reason, "grid_id": self.coverage.grid_id}
                if self.coverage else None
            ),
            "config": {
                "revision": self.cfg.revision,
                "warnings": list(self.cfg.warnings),
                "error": self.config_error,
                "restart_required": list(self.pending_restart),
            },
            "maintenance": {"pending": self.maintenance, "allow_reboot": self.cfg.maintenance.allow_reboot},
            "tx_permitted": self.tx_permitted,
        }
        with self._status_lock:
            self._status = snap

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        """Request the configured Monitor mode (or ``no_tx``) with bounded time."""
        self.notifier.stopping()
        target = "no_tx" if self.latch.active else self.cfg.policy.monitor_mode
        if self.observed_mode != target:
            try:
                self.openhop.set_mode(target)
                log.info("shutdown: requested OpenHop %s", target)
            except OpenHopError as exc:
                log.error("shutdown: could not request %s: %s", target, exc)
        if self.meshmapper is not None:
            self.meshmapper.stop()
        log.info("controller stopped")
