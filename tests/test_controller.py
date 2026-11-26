"""Controller behavior with fake GPS, OpenHop, and clock (design tests 3, 5, 6, 13, 15, 17)."""

import tempfile
import unittest
from pathlib import Path

from fakes import (
    AUTH, UNAVAILABLE, FakeClock, FakeGps, FakeOpenHop, MemStore,
    MemTxLockoutStore, RecordingNotifier,
)

from openhop_geofence import config
from openhop_geofence.controller import CommandError, Controller
from openhop_geofence.state import SafeModeLatch, TxLockout

CONFIG = """
[policy]
outside = "{outside}"
monitor_mode = "{monitor_mode}"
repeat_delay_seconds = 10
minimum_monitor_seconds = 30

[maintenance]
allow_reboot = {allow_reboot}

[[zones]]
id = "repeat-area"
role = "repeat"
latitude = 40.0
longitude = -105.0
radius_m = 1000

[[zones]]
id = "quiet"
role = "monitor"
latitude = 40.0
longitude = -105.0
radius_m = 100
"""

IN_REPEAT = (40.005, -105.0)
IN_MONITOR = (40.0, -105.0)
OUTSIDE = (41.0, -105.0)


class ControllerTestCase(unittest.TestCase):
    outside = "monitor"
    monitor_mode = "monitor"
    allow_reboot = "false"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(CONFIG.format(outside=self.outside, monitor_mode=self.monitor_mode,
                                           allow_reboot=self.allow_reboot))
        self.clock = FakeClock()
        self.gps = FakeGps(self.clock)
        self.oh = FakeOpenHop("monitor")
        self.store = MemStore()
        self.tx_store = MemTxLockoutStore()
        self.notifier = RecordingNotifier()
        self.reboots = []
        self.events = []
        self.c = self.make()

    def make(self):
        c = Controller(
            config.load_config(self.path), self.path, gps=self.gps, safe_store=self.store,
            tx_lockout_store=self.tx_store,
            make_openhop=lambda cfg: self.oh, notifier=self.notifier, clock=self.clock,
            wall_clock=self.clock.wall, time_trusted=lambda: True,
            reboot=lambda: (self.reboots.append(1) or (True, "ok")),
        )
        c.listeners.append(self)
        return c

    def on_event(self, event):
        self.events.append(event)

    def tick(self, seconds=1.0, n=1):
        for _ in range(n):
            self.clock.advance(seconds)
            self.c.step()

    def do(self, action, **kw):
        fut = self.c.submit(action, requester="test", **kw)
        self.c._run_command(*self.c._commands.get_nowait())
        return fut.result()

    def at(self, pos):
        self.gps.set(*pos)


class AutomaticControlTests(ControllerTestCase):
    def test_missing_startup_gps_uses_monitor_without_safe_mode(self):
        self.c.step()
        self.assertFalse(self.c.latch.active)
        self.assertEqual(self.oh.mode, "monitor")

    def test_repeat_zone_after_hysteresis_and_monitor_zone_priority(self):
        self.at(IN_REPEAT)
        self.c.step()
        self.assertEqual(self.oh.mode, "monitor")  # Startup: Monitor until Repeat is eligible.
        self.tick(1, 29)
        self.assertEqual(self.oh.mode, "monitor")
        self.tick(1, 2)
        self.assertEqual(self.oh.mode, "forward")
        self.at(IN_MONITOR)
        self.tick()
        self.assertEqual(self.oh.mode, "monitor")

    def test_gps_loss_selects_monitor_but_keeps_override(self):
        self.at(IN_REPEAT)
        self.tick(1, 31)
        self.assertEqual(self.oh.mode, "forward")
        self.gps.set(None)
        self.tick()
        self.assertEqual(self.oh.mode, "monitor")
        self.do("override", mode="forward")
        self.assertEqual(self.oh.mode, "forward")
        self.tick(1, 5)
        self.assertEqual(self.oh.mode, "forward")

    def test_unchanged_mode_is_not_rewritten(self):
        self.at(OUTSIDE)
        self.tick(1, 20)
        self.assertEqual(self.oh.writes, [])

    def test_watchdog_only_after_successful_evaluation(self):
        self.at(OUTSIDE)
        self.c.step()
        self.assertIn("WATCHDOG=1", self.notifier.messages)
        self.notifier.messages.clear()
        self.c._evaluate = lambda: 1 / 0
        self.c.step()
        self.assertNotIn("WATCHDOG=1", self.notifier.messages)
        self.assertTrue(self.c.latch.active)


class NoTxMappingTests(ControllerTestCase):
    monitor_mode = "no_tx"

    def test_monitor_maps_to_no_tx(self):
        self.at(OUTSIDE)
        self.c.step()
        self.assertEqual(self.oh.mode, "no_tx")


class LastPolicyTests(ControllerTestCase):
    outside = "last"

    def test_last_holds_confirmed_zone_result(self):
        self.oh.mode = "forward"
        self.at(OUTSIDE)
        self.c.step()
        # First OpenHop read sets the held result, but startup still waits for eligibility.
        self.tick(1, 31)
        self.assertEqual(self.oh.mode, "forward")
        self.at(IN_MONITOR)
        self.tick()
        self.at(OUTSIDE)
        self.tick(1, 40)
        self.assertEqual(self.oh.mode, "monitor")


class SafeModeTests(ControllerTestCase):
    def test_disable_tx_uses_operator_lockout_not_safe_mode(self):
        self.at(OUTSIDE)
        self.c.step()
        self.do("disable_tx")
        self.assertEqual(self.oh.mode, "no_tx")
        self.assertFalse(self.store.latch.active)
        self.assertTrue(self.tx_store.lockout.active)
        with self.assertRaises(CommandError):
            self.do("override", mode="forward")
        with self.assertRaises(CommandError):
            self.do("resume")

    def test_tx_lockout_persists_across_restart_and_clears_separately(self):
        self.tx_store.lockout = TxLockout(True, "Disable all TX requested", None)
        self.c = self.make()
        self.at(IN_REPEAT)
        self.c.step()
        self.assertEqual(self.oh.mode, "no_tx")
        self.assertFalse(self.c.latch.active)
        self.assertIn("cleared", self.do("clear_tx_lockout"))
        self.assertFalse(self.tx_store.lockout.active)
        self.assertEqual(self.oh.mode, "monitor")

    def test_latch_persists_across_restart(self):
        self.store.latch = SafeModeLatch(True, "earlier fault", None)
        self.c = self.make()
        self.at(IN_REPEAT)
        self.c.step()
        self.assertEqual(self.oh.mode, "no_tx")

    def test_clear_requires_checks(self):
        self.c._on_fault("test fault", announce_failure=False)
        self.gps.set(None)
        with self.assertRaisesRegex(CommandError, "GPS"):
            self.do("clear")
        self.at(OUTSIDE)
        self.oh.fail = UNAVAILABLE
        with self.assertRaisesRegex(CommandError, "OpenHop"):
            self.do("clear")
        self.oh.fail = None
        self.path.write_text(self.path.read_text() + "\n# edited\n")
        with self.assertRaisesRegex(CommandError, "reload"):
            self.do("clear")
        self.do("reload")
        self.store.fail = True
        with self.assertRaisesRegex(CommandError, "writable"):
            self.do("clear")
        self.store.fail = False
        self.assertIn("cleared", self.do("clear"))
        self.assertFalse(self.store.latch.active)
        self.tick()
        self.assertEqual(self.oh.mode, "monitor")

    def test_clearing_safe_mode_does_not_claim_resume_while_tx_locked(self):
        self.store.latch = SafeModeLatch(True, "earlier fault", None)
        self.tx_store.lockout = TxLockout(True, "operator request", None)
        self.c = self.make()
        self.at(IN_REPEAT)
        self.c.step()
        result = self.do("clear")
        self.assertIn("operator TX lockout remains active", result)
        self.assertFalse(self.store.latch.active)
        self.assertTrue(self.tx_store.lockout.active)
        self.assertEqual(self.oh.mode, "no_tx")

    def test_persistence_failure_still_requests_no_tx(self):
        self.tx_store.fail = True
        self.assertIn("WARNING", self.do("disable_tx"))
        self.assertEqual(self.oh.mode, "no_tx")

    def test_external_change_is_a_control_conflict(self):
        self.at(OUTSIDE)
        self.c.step()
        self.oh.mode = "forward"  # Someone else changed the mode.
        self.tick(6)
        self.assertTrue(self.c.latch.active)
        self.assertIn("conflict", self.c.latch.cause)
        self.tick()  # no_tx is requested on the next evaluation (within one second).
        self.assertEqual(self.oh.mode, "no_tx")

    def test_repeated_auth_errors_fault(self):
        self.at(OUTSIDE)
        self.oh.fail = AUTH
        for _ in range(4):
            self.tick(60)
        self.assertTrue(self.c.latch.active)

    def test_outage_is_not_a_fault_and_reconnect_restarts_eligibility(self):
        self.at(IN_REPEAT)
        self.tick(1, 31)
        self.oh.fail = UNAVAILABLE
        self.tick(6, 5)
        self.assertFalse(self.c.latch.active)
        self.assertIsNone(self.c.status()["openhop"]["observed_mode"])
        self.oh.fail = None
        self.tick(30, 2)
        self.assertEqual(self.oh.mode, "monitor")  # Monitor first after reconnect.

    def test_announce_failure_grace_delays_no_tx(self):
        sent = []

        class Gasp:
            def announce_failure_ready(self):
                return True

            def send_announce_failure(self, text):
                sent.append(text)

        self.c.announce_failure = Gasp()
        self.at(OUTSIDE)
        self.c.step()
        self.oh.mode = "forward"
        self.tick(6)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.oh.mode, "forward")  # Within the grace period.
        self.tick(11)
        self.assertEqual(self.oh.mode, "no_tx")

    def test_disable_tx_cancels_grace(self):
        class Gasp:
            def announce_failure_ready(self):
                return True

            def send_announce_failure(self, text):
                pass

        self.c.announce_failure = Gasp()
        self.at(OUTSIDE)
        self.c.step()
        self.oh.mode = "forward"
        self.tick(6)
        self.do("disable_tx")
        self.assertEqual(self.oh.mode, "no_tx")


class ConfigCommandTests(ControllerTestCase):
    def test_save_config_revision_and_roles(self):
        text = self.path.read_text()
        rev = config.config_revision(text)
        with self.assertRaisesRegex(CommandError, "conflict"):
            self.do("save_config", text=text, revision="stale", role="admin")
        with self.assertRaisesRegex(CommandError, "administrator"):
            self.do("save_config", text=text.replace("allow_reboot = false", "allow_reboot = true"),
                    revision=rev, role="operator")
        with self.assertRaisesRegex(CommandError, "administrator"):
            self.do("save_config", text=text + '\n[openhop]\nbase_url = "http://10.0.0.1:8000"\n',
                    revision=rev, role="operator")
        with self.assertRaises(CommandError):
            self.do("save_config", text="[policy]\noutside = 'bogus'\n", revision=rev, role="admin")
        self.assertEqual(self.path.read_text(), text)  # Rejected saves leave the file alone.
        new = text.replace('outside = "monitor"', 'outside = "repeat"')
        self.assertIn("applied", self.do("save_config", text=new, revision=rev, role="operator"))
        self.assertEqual(self.c.cfg.policy.outside, "repeat")
        rev = config.config_revision(new)
        self.assertIn("applied", self.do("save_config", text=new.replace("allow_reboot = false", "allow_reboot = true"),
                                         revision=rev, role="admin"))
        self.assertTrue(self.c.cfg.maintenance.allow_reboot)

    def test_managed_secret_update_and_invalid_paths(self):
        directory = self.path.parent / "secrets"
        directory.mkdir()
        secret = directory / "openhop.key"
        text = self.path.read_text() + f'\n[openhop]\napi_key_file = "{secret}"\n'
        self.path.write_text(text)
        self.c.cfg = config.load_config(self.path)
        self.c._connected = True
        loaded_keys = []
        self.c._make_openhop = lambda cfg: loaded_keys.append(config.read_secret(cfg.openhop.api_key_file)) or self.oh
        self.assertIn("saved and applied", self.do("update_secret", kind="openhop", value="api-key"))
        self.assertEqual(secret.read_text(), "api-key\n")
        self.assertEqual(secret.stat().st_mode & 0o777, 0o640)
        self.assertEqual(loaded_keys, ["api-key"])
        self.assertFalse(self.c._connected)
        with self.assertRaises(CommandError):
            self.do("update_secret", kind="openhop", value="bad\nkey")
        self.assertEqual(secret.read_text(), "api-key\n")
        secret.unlink()
        outside = self.path.parent / "outside"
        secret.symlink_to(outside)
        with self.assertRaisesRegex(CommandError, "non-symlink"):
            self.do("update_secret", kind="openhop", value="other-key")
        self.assertFalse(outside.exists())

    def test_meshmapper_secret_replaces_worker(self):
        directory = self.path.parent / "secrets"
        directory.mkdir()
        key = directory / "meshmapper.key"
        text = self.path.read_text() + f'\n[meshmapper]\napi_key_file = "{key}"\n'
        self.path.write_text(text)
        self.c.cfg = config.load_config(self.path)
        calls = []

        class Worker:
            def stop(self):
                calls.append("stopped")

            def start(self):
                calls.append("started")

        self.c.meshmapper = Worker()
        self.c._make_meshmapper = lambda cfg: calls.append(config.read_secret(cfg.meshmapper.api_key_file)) or Worker()
        self.do("update_secret", kind="meshmapper", value="new-key")
        self.assertEqual(calls, ["stopped", "new-key", "started"])

    def test_invalid_reload_latches_safe_mode(self):
        self.path.write_text("[policy]\noutside = 'bogus'\n")
        with self.assertRaises(CommandError):
            self.do("reload")
        self.assertTrue(self.c.latch.active)


class MaintenanceTests(ControllerTestCase):
    def test_restart_sets_exit_status_once(self):
        self.do("restart")
        self.assertEqual(self.c.exit_status, 75)
        with self.assertRaises(CommandError):
            self.do("restart")

    def test_reboot_can_be_disabled(self):
        with self.assertRaisesRegex(CommandError, "disabled"):
            self.do("reboot")
        self.assertEqual(self.reboots, [])


class RebootEnabledTests(ControllerTestCase):
    allow_reboot = "true"

    def test_reboot_requests_os_interface_and_preserves_latch(self):
        self.do("disable_tx")
        self.assertIn("scheduled", self.do("reboot"))
        self.assertEqual(self.reboots, [1])
        self.assertTrue(self.tx_store.lockout.active)
        self.assertFalse(self.store.latch.active)

    def test_reboot_defaults_to_enabled(self):
        self.path.write_text(self.path.read_text().replace("allow_reboot = true", ""))
        self.c = self.make()
        self.assertIn("scheduled", self.do("reboot"))
        self.assertEqual(self.reboots, [1])


if __name__ == "__main__":
    unittest.main()
