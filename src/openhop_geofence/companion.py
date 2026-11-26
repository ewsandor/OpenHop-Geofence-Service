"""Optional MeshCore companion: status reports and remote control over RF.

The companion worker runs its own asyncio event loop in a background thread
and uses the MeshCore Python library (``meshcore``) for transport, framing,
message retrieval, and contacts. It never writes the radio mode itself: all
control goes through :meth:`Controller.submit`, exactly like the web
interface.

Transmission rules (DESIGN.md section 10):

* When the companion shares the controlled OpenHop radio
  (``shares_openhop_radio = true``), nothing is sent unless the controller
  reports that TX is permitted. Suppressed replies are logged, never queued.
* Incoming DMs are still processed while blocked so an authorized ``clear``
  can be received.
* During an eligible fault, a bounded number of failure announcement channel messages
  may be submitted before the controller's grace deadline.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import logging
import threading
import time
from typing import Any, Callable

from .config import REPORT_LEVELS, CompanionConfig, read_secret

log = logging.getLogger(__name__)

MAX_TEXT = 140  # Conservative MeshCore text length
REPORT_EXPIRY_SECONDS = 600
MAX_PENDING_REPORTS = 8
COMMAND_WINDOW_SECONDS = 60
MAX_COMMANDS_PER_WINDOW = 10
COMMAND_MAX_AGE_SECONDS = 300
DEDUP_SIZE = 64
RECONNECT_MAX_SECONDS = 300
CLEAR_CONFIRM_WAIT_SECONDS = 15

CHANNEL_COMMANDS = ("help", "status")
CONTROL_COMMANDS = ("repeat", "monitor", "notx", "txon", "resume", "clear")
ADMIN_COMMANDS = ("restart", "reboot")
DM_COMMANDS = CHANNEL_COMMANDS + ("debug",) + CONTROL_COMMANDS + ADMIN_COMMANDS


def meshcore_available() -> bool:
    try:
        import meshcore  # noqa: F401
    except ImportError:
        return False
    return True


def _level_rank(level: str) -> int:
    return REPORT_LEVELS.index(level) if level in REPORT_LEVELS else 0


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


class Companion:
    """MeshCore companion client: reporting, commands, and failure announcement messages."""

    def __init__(
        self,
        cfg: CompanionConfig,
        controller: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        time_trusted: Callable[[], bool] = lambda: False,
        connect: Callable[[], Any] | None = None,
    ):
        self.cfg = cfg
        self.controller = controller
        self.clock = clock
        self.wall = wall_clock
        self.time_trusted = time_trusted
        self._connect_override = connect
        self.start_wall = wall_clock()

        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False
        self.mc: Any = None
        self.connected = False
        self.error: str | None = None if meshcore_available() or connect else "meshcore library not installed"
        self.public_key: str | None = None
        self.node_name: str | None = None
        self.contact_uri: str | None = None
        self.identity_warning: str | None = None
        self.last_send: dict | None = None
        self.suppressed = 0

        self._pending: collections.OrderedDict[str, tuple[str, str, float]] = collections.OrderedDict()
        self._report_wake: asyncio.Event | None = None
        self._last_report_at = -1e18
        self._seen: collections.deque[tuple] = collections.deque(maxlen=DEDUP_SIZE)
        self._command_times: collections.deque[float] = collections.deque()
        self._announce_failure_sent = False

    # ------------------------------------------------------------------
    # Lifecycle (called from the main thread)
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="companion", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping = True
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._cancel_all)
        if self._thread is not None:
            self._thread.join(timeout)

    def _cancel_all(self) -> None:
        for task in asyncio.all_tasks(self.loop):
            task.cancel()

    def _thread_main(self) -> None:
        self.loop = asyncio.new_event_loop()
        try:
            self.loop.run_until_complete(self._main())
        except asyncio.CancelledError:
            pass
        finally:
            self.loop.run_until_complete(self._disconnect())
            self.loop.close()

    # ------------------------------------------------------------------
    # Controller hooks (called from the controller thread; non-blocking)
    # ------------------------------------------------------------------

    def on_event(self, event: Any) -> None:
        """Queue a controller event for reporting if it meets ``report_level``."""
        if not self.cfg.report_channels or _level_rank(event.level) < _level_rank(self.cfg.report_level):
            return
        if self.loop is not None and not self._stopping:
            self.loop.call_soon_threadsafe(self._add_report, event.kind, event.level, event.message)

    def announce_failure_ready(self) -> bool:
        return (
            self.cfg.announce_failure_enabled
            and bool(self.cfg.report_channels)
            and self.connected
            and self.loop is not None
            and not self._stopping
        )

    def send_announce_failure(self, text: str) -> None:
        """Submit bounded failure announcement messages without waiting for delivery."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(lambda: self.loop.create_task(self._announce_failure(text)))

    def status(self) -> dict:
        return {
            "enabled": True,
            "transport": self.cfg.transport,
            "connected": self.connected,
            "error": self.error,
            "public_key": self.public_key,
            "node_name": self.node_name,
            "contact_uri": self.contact_uri,
            "identity_warning": self.identity_warning,
            "report_channels": list(self.cfg.report_channels),
            "report_level": self.cfg.report_level,
            "authorization": self.cfg.command_authorization,
            "admin_keys": len(self.cfg.admin_public_keys),
            "shares_openhop_radio": self.cfg.shares_openhop_radio,
            "tx_allowed": self._tx_allowed(),
            "last_send": self.last_send,
            "suppressed_messages": self.suppressed,
        }

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def _open(self) -> Any:
        if self._connect_override is not None:
            return await self._connect_override()
        from meshcore import MeshCore

        c = self.cfg
        if c.transport == "tcp":
            return await MeshCore.create_tcp(c.host, c.port)
        if c.transport == "serial":
            return await MeshCore.create_serial(c.serial_device, c.serial_baud)
        return await MeshCore.create_ble(c.ble_address, pin=read_secret(c.ble_pin_file))

    async def _main(self) -> None:
        self._report_wake = asyncio.Event()
        reporter = asyncio.create_task(self._report_loop())
        backoff = 5.0
        try:
            while not self._stopping:
                try:
                    await self._session()
                    backoff = 5.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # Library and transport errors vary widely.
                    if self.error != str(exc):
                        log.warning("companion connection failed: %s", exc)
                    self.error = str(exc) or exc.__class__.__name__
                self.connected = False
                await self._disconnect()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
        finally:
            reporter.cancel()

    async def _session(self) -> None:
        from meshcore import EventType

        if not meshcore_available() and self._connect_override is None:
            raise RuntimeError("meshcore library not installed (see README)")
        self.mc = await self._open()
        if self.mc is None:
            raise ConnectionError(f"no response from companion ({self.cfg.transport})")
        lost = asyncio.Event()
        self.mc.subscribe(EventType.DISCONNECTED, lambda _e: lost.set())
        self.mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_dm)
        self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel)
        info = self.mc.self_info or {}
        self.public_key = info.get("public_key")
        self.node_name = info.get("name")
        try:
            uri = await self.mc.commands.export_contact()
            payload = getattr(uri, "payload", None) or {}
            self.contact_uri = payload.get("uri") if isinstance(payload, dict) else None
        except Exception as exc:
            log.info("could not export companion contact: %s", exc)
        await self._check_identity()
        await self.mc.start_auto_message_fetching()
        self.connected, self.error = True, None
        log.info("companion connected via %s (%s)", self.cfg.transport, self.node_name or "unnamed")
        # Report the current state rather than replaying outage history.
        self._pending.clear()
        if _level_rank(self.cfg.report_level) == 0 and self.cfg.report_channels:
            self._add_report("connected", "info", "Geofence online. " + self.status_text())
        await lost.wait()
        raise ConnectionError("companion disconnected")

    async def _disconnect(self) -> None:
        mc, self.mc = self.mc, None
        self.connected = False
        if mc is not None:
            try:
                await asyncio.wait_for(mc.disconnect(), 5)
            except Exception:
                pass

    async def _check_identity(self) -> None:
        """Verify the configured OpenHop companion name matches this identity."""
        name = self.cfg.openhop_companion_name
        openhop = getattr(self.controller, "openhop", None)
        if not name or openhop is None or not self.public_key:
            return
        try:
            companions = await asyncio.get_running_loop().run_in_executor(None, openhop.list_companions)
        except Exception as exc:
            self.identity_warning = f"could not verify companion identity: {exc}"
            return
        match = [c for c in companions if c.get("companion_name") == name or c.get("node_name") == name]
        keys = {str(c.get("public_key", "")).lower() for c in match}
        if not match:
            self.identity_warning = f"OpenHop has no companion named {name!r}"
        elif self.public_key.lower() not in keys:
            self.identity_warning = f"connected identity does not match OpenHop companion {name!r}"
        else:
            self.identity_warning = None
        if self.identity_warning:
            log.warning("%s", self.identity_warning)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _tx_allowed(self) -> bool:
        return not self.cfg.shares_openhop_radio or bool(getattr(self.controller, "tx_permitted", False))

    async def _send(self, target: str, text: str, *, announce_failure: bool = False) -> bool:
        """Send to ``ch:<idx>`` or ``dm:<key>``. Honors the shared-radio gate."""
        text = _clip(text)
        lockout = getattr(self.controller, "tx_lockout", None)
        if self.cfg.shares_openhop_radio and getattr(lockout, "active", False):
            self.suppressed += 1
            log.info("companion message suppressed (operator TX lockout): %s", text)
            return False
        if not announce_failure and not self._tx_allowed():
            self.suppressed += 1
            log.info("companion message suppressed (shared radio TX not permitted): %s", text)
            return False
        if self.mc is None or not self.connected:
            return False
        kind, _, dest = target.partition(":")
        try:
            if kind == "ch":
                result = await self.mc.commands.send_chan_msg(int(dest), text)
            else:
                result = await self.mc.commands.send_msg(dest, text)
        except Exception as exc:
            result = exc
        etype = getattr(getattr(result, "type", None), "name", None)
        ok = not isinstance(result, Exception) and etype not in ("ERROR", None)
        self.last_send = {"target": kind, "ok": ok, "monotonic": self.clock()}
        if not ok:
            # Local log only; never report a send failure through the same link.
            log.warning("companion send to %s failed: %s", kind, getattr(result, "payload", result))
        return ok

    async def _announce_failure(self, text: str) -> None:
        if self._announce_failure_sent:
            return  # At most one failure announcement burst per process.
        self._announce_failure_sent = True
        channels = self.cfg.report_channels
        attempts = min(self.cfg.announce_failure_max_attempts, 3)
        for i in range(attempts):
            await self._send(f"ch:{channels[i % len(channels)]}", text, announce_failure=True)
        log.warning("failure announcement: %d message(s) submitted", attempts)

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def _add_report(self, kind: str, level: str, message: str) -> None:
        # Coalesce by kind: the newest message of each kind replaces the older one.
        self._pending.pop(kind, None)
        self._pending[kind] = (level, message, self.clock())
        while len(self._pending) > MAX_PENDING_REPORTS:
            lowest = min(self._pending, key=lambda k: _level_rank(self._pending[k][0]))
            del self._pending[lowest]
        if self._report_wake is not None:
            self._report_wake.set()

    async def _report_loop(self) -> None:
        assert self._report_wake is not None
        while True:
            await self._report_wake.wait()
            self._report_wake.clear()
            while self._pending:
                now = self.clock()
                for k in [k for k, v in self._pending.items() if now - v[2] > REPORT_EXPIRY_SECONDS]:
                    del self._pending[k]
                if not self._pending:
                    break
                # Errors first, then oldest.
                kind = max(self._pending, key=lambda k: (_level_rank(self._pending[k][0]), -self._pending[k][2]))
                level, message, _ = self._pending[kind]
                interval = self.cfg.minimum_report_interval_seconds
                if level == "error":
                    interval = min(interval, 5.0)
                wait = self._last_report_at + interval - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue
                del self._pending[kind]
                if not self.connected:
                    continue  # Reconnect reports current state instead.
                self._last_report_at = self.clock()
                for ch in self.cfg.report_channels:
                    await self._send(f"ch:{ch}", f"[{level.upper()}] {message}")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def status_text(self) -> str:
        s = self.controller.status()
        c, o, g = s.get("control", {}), s.get("openhop", {}), s.get("gps", {})
        cov = s.get("coverage") or {}
        parts = [
            f"{c.get('state', '?')}",
            f"mode {o.get('observed_mode') or 'unknown'}",
            f"want {c.get('desired_mode') or '?'}",
            f"GPS {'fix' if g.get('fix') else 'none'}",
        ]
        zones = (s.get("zones") or {})
        names = list(zones.get("monitor", [])) + list(zones.get("repeat", []))
        if names:
            parts.append("zone " + ",".join(names)[:30])
        elif cov:
            parts.append(f"cov {cov.get('classification') or 'n/a'}")
        if s.get("safe_mode", {}).get("active"):
            parts.append("SAFE MODE")
        if s.get("tx_lockout", {}).get("active"):
            parts.append("TX LOCKOUT")
        return "; ".join(parts)

    def debug_text(self) -> str:
        s = self.controller.status()
        c, o, g = s.get("control", {}), s.get("openhop", {}), s.get("gps", {})
        sm = s.get("safe_mode", {})
        cov = s.get("coverage") or {}
        parts = [
            f"src {c.get('source')}: {c.get('reason')}",
            f"OH {o.get('error') or 'ok'}",
            f"GPS {g.get('error') or 'ok'}",
            f"MM {cov.get('reason') or 'n/a'}",
        ]
        if sm.get("active"):
            parts.append(f"safe: {sm.get('cause')}")
        if lockout := s.get("tx_lockout", {}):
            if lockout.get("active"):
                parts.append(f"tx lockout: {lockout.get('cause')}")
        if not s.get("time_trusted"):
            parts.append("time untrusted")
        return "; ".join(parts)

    def _parse(self, text: str) -> tuple[str, list[str]] | None:
        prefix = self.cfg.command_prefix.lower()
        words = text.strip().split()
        if not words or words[0].lower() != prefix:
            return None
        if len(words) == 1:
            return "help", []
        return words[1].lower(), words[2:]

    def _help(self, dm: bool) -> str:
        cmds = DM_COMMANDS if dm else CHANNEL_COMMANDS
        return f"{self.cfg.command_prefix} " + "|".join(cmds)

    def _authorize(self, prefix: str) -> tuple[bool, bool, str | None, str]:
        """Return (control_allowed, is_admin, full_key, reason)."""
        prefix = prefix.lower()
        matches = [k.lower() for k in self.cfg.admin_public_keys if k.lower().startswith(prefix)]
        if len(matches) > 1:
            return False, False, None, "ambiguous sender identity"
        admin = len(matches) == 1
        full = matches[0] if admin else None
        if admin:
            return True, True, full, ""
        if self.cfg.command_authorization == "any_sender":
            return True, False, None, ""
        return False, False, None, "not authorized"

    def _rate_limited(self) -> bool:
        now = self.clock()
        while self._command_times and now - self._command_times[0] > COMMAND_WINDOW_SECONDS:
            self._command_times.popleft()
        if len(self._command_times) >= MAX_COMMANDS_PER_WINDOW:
            return True
        self._command_times.append(now)
        return False

    def _fresh(self, sender_ts: Any) -> str | None:
        """Return a rejection reason if a control message cannot be validated as fresh."""
        if not self.time_trusted():
            return "system time is not synchronized; control rejected"
        if not isinstance(sender_ts, (int, float)):
            return "message has no timestamp"
        now = self.wall()
        if sender_ts < self.start_wall - 30:
            return "message predates service start"
        if abs(now - sender_ts) > COMMAND_MAX_AGE_SECONDS:
            return "stale message"
        return None

    def _on_dm(self, event: Any) -> None:
        asyncio.get_running_loop().create_task(self.handle_dm(event.payload or {}))

    def _on_channel(self, event: Any) -> None:
        asyncio.get_running_loop().create_task(self.handle_channel(event.payload or {}))

    async def handle_channel(self, payload: dict) -> None:
        idx = payload.get("channel_idx")
        if idx not in self.cfg.report_channels:
            return
        text = str(payload.get("text", ""))[:200]
        # Channel text is usually "Name: message"; ignore our own messages.
        sender, sep, body = text.partition(": ")
        if not sep:
            body = text
        elif self.node_name and sender == self.node_name:
            return
        parsed = self._parse(body)
        if parsed is None or self._rate_limited():
            return
        cmd, _ = parsed
        if cmd == "status":
            reply = self.status_text()
        elif cmd == "help":
            reply = self._help(dm=False)
        else:
            reply = "Control commands require an authorized DM"
        await self._send(f"ch:{idx}", reply)

    async def handle_dm(self, payload: dict) -> None:
        prefix = str(payload.get("pubkey_prefix", "")).lower()
        text = str(payload.get("text", ""))[:200]
        if not prefix or (self.public_key and self.public_key.lower().startswith(prefix)):
            return
        key = (prefix, payload.get("sender_timestamp"), text)
        if key in self._seen:
            return
        self._seen.append(key)
        parsed = self._parse(text)
        if parsed is None or self._rate_limited():
            return
        cmd, _ = parsed
        allowed, admin, full_key, why = self._authorize(prefix)
        reply_to = f"dm:{full_key or prefix}"
        if cmd not in DM_COMMANDS:
            await self._send(reply_to, f"Unknown command. {self._help(True)}")
            return
        if cmd == "help":
            await self._send(reply_to, self._help(True))
            return
        if cmd == "status":
            await self._send(reply_to, self.status_text())
            return
        if not allowed or (cmd in ADMIN_COMMANDS and not admin):
            log.warning("companion command %s from %s rejected: %s", cmd, prefix, why or "administrator required")
            await self._send(reply_to, f"Rejected: {why or 'administrator required'}")
            return
        if cmd == "debug":
            await self._send(reply_to, self.debug_text())
            return
        stale = self._fresh(payload.get("sender_timestamp"))
        if stale:
            log.warning("companion command %s from %s rejected: %s", cmd, prefix, stale)
            await self._send(reply_to, f"Rejected: {stale}")
            return
        action, kwargs = {
            "repeat": ("override", {"mode": "forward"}),
            "monitor": ("override", {"mode": "monitor"}),
            "notx": ("disable_tx", {}),
            "txon": ("clear_tx_lockout", {}),
            "resume": ("resume", {}),
            "clear": ("clear", {}),
            "restart": ("restart", {}),
            "reboot": ("reboot", {}),
        }[cmd]
        future: concurrent.futures.Future = self.controller.submit(
            action, requester=f"companion DM {prefix}", **kwargs
        )
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(future), 30)
            ok = True
        except Exception as exc:
            result, ok = str(exc) or exc.__class__.__name__, False
        log.info("companion command %s from %s: %s", cmd, prefix, result)
        if cmd in ("clear", "txon") and ok:
            # A clear confirmation is sent only once readback permits TX.
            deadline = self.clock() + CLEAR_CONFIRM_WAIT_SECONDS
            while not self._tx_allowed() and self.clock() < deadline:
                await asyncio.sleep(1)
        await self._send(reply_to, result if ok else f"Failed: {result}")
