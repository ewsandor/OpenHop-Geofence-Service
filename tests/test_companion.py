"""Companion command authorization, freshness, and send gating (design test 15)."""

import asyncio
import concurrent.futures
import unittest
from dataclasses import replace
from types import SimpleNamespace

from fakes import FakeClock

from openhop_geofence.companion import MAX_TEXT, Companion
from openhop_geofence.config import CompanionConfig

ADMIN = "a1b2c3" + "0" * 58
OTHER = "a1b2ff" + "0" * 58


class FakeCommands:
    def __init__(self):
        self.sent = []

    async def send_msg(self, dest, text):
        self.sent.append(("dm", dest, text))
        return SimpleNamespace(type=SimpleNamespace(name="MSG_SENT"))

    async def send_chan_msg(self, idx, text):
        self.sent.append(("ch", idx, text))
        return SimpleNamespace(type=SimpleNamespace(name="OK"))


class FakeController:
    def __init__(self):
        self.tx_permitted = True
        self.submitted = []

    def submit(self, action, **kw):
        self.submitted.append((action, kw))
        f = concurrent.futures.Future()
        f.set_result(f"{action} accepted")
        return f

    def status(self):
        return {"control": {"state": "repeat", "desired_mode": "forward"},
                "openhop": {"observed_mode": "forward"}, "gps": {"fix": True}, "time_trusted": True}


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.ctl = FakeController()
        self.trusted = True
        self.cfg = CompanionConfig(enabled=True, admin_public_keys=[ADMIN, OTHER], report_channels=[0, 1])
        self.comp = self.make(self.cfg)

    def make(self, cfg):
        comp = Companion(cfg, self.ctl, clock=self.clock, wall_clock=self.clock.wall,
                         time_trusted=lambda: self.trusted, connect=lambda: None)
        comp.mc = SimpleNamespace(commands=FakeCommands())
        comp.connected = True
        comp.public_key = "ffff" + "0" * 60
        return comp

    @property
    def sent(self):
        return self.comp.mc.commands.sent

    def dm(self, text, prefix=ADMIN[:12], ts=None):
        ts = self.clock.wall() if ts is None else ts
        asyncio.run(self.comp.handle_dm({"pubkey_prefix": prefix, "sender_timestamp": ts, "text": text}))

    def test_admin_control_command(self):
        self.dm("!geofence repeat")
        self.assertEqual(self.ctl.submitted, [("override", {"mode": "forward", "requester": f"companion DM {ADMIN[:12]}"})])
        self.assertEqual(self.sent[-1], ("dm", ADMIN, "override accepted"))

    def test_ambiguous_prefix_rejected(self):
        self.dm("!geofence repeat", prefix="a1b2")
        self.assertEqual(self.ctl.submitted, [])
        self.assertIn("ambiguous", self.sent[-1][2])

    def test_unknown_sender_rejected_but_status_allowed(self):
        self.dm("!geofence notx", prefix="deadbeef0000")
        self.assertEqual(self.ctl.submitted, [])
        self.assertIn("not authorized", self.sent[-1][2])
        self.dm("!geofence status", prefix="deadbeef0000")
        self.assertIn("repeat", self.sent[-1][2])

    def test_admin_commands_need_admin_even_with_any_sender(self):
        self.comp = self.make(replace(self.cfg, command_authorization="any_sender"))
        self.dm("!geofence monitor", prefix="deadbeef0000")
        self.dm("!geofence reboot", prefix="deadbeef0001")
        self.assertEqual([a for a, _ in self.ctl.submitted], ["override"])
        self.assertIn("administrator", self.sent[-1][2])

    def test_reboot_requires_fresh_admin_dm(self):
        self.dm("!geofence reboot", ts=self.clock.wall() - 600)
        self.assertEqual(self.ctl.submitted, [])
        self.dm("!geofence reboot")
        self.assertEqual([action for action, _ in self.ctl.submitted], ["reboot"])
        self.dm("!geofence reboot")
        self.assertEqual(len(self.ctl.submitted), 1)

    def test_stale_untrusted_and_duplicate_messages(self):
        self.dm("!geofence notx", ts=self.clock.wall() + 600)  # Clock skew beyond limit.
        self.assertIn("stale", self.sent[-1][2])
        self.trusted = False
        self.dm("!geofence notx")
        self.assertIn("not synchronized", self.sent[-1][2])
        self.trusted = True
        ts = self.clock.wall() + 1
        self.dm("!geofence notx", ts=ts)
        self.dm("!geofence notx", ts=ts)  # Retransmission: ignored.
        self.assertEqual(len(self.ctl.submitted), 1)

    def test_channel_refuses_control(self):
        asyncio.run(self.comp.handle_channel({"channel_idx": 0, "text": "Bob: !geofence repeat"}))
        self.assertEqual(self.ctl.submitted, [])
        self.assertEqual(self.sent[-1][:2], ("ch", 0))
        asyncio.run(self.comp.handle_channel({"channel_idx": 5, "text": "Bob: !geofence status"}))
        self.assertEqual(len(self.sent), 1)  # Not a report channel: ignored.

    def test_shared_radio_gate_and_announce_failure(self):
        self.ctl.tx_permitted = False
        self.dm("!geofence status")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.comp.suppressed, 1)
        asyncio.run(self.comp._announce_failure("x" * 500))
        asyncio.run(self.comp._announce_failure("again"))  # One burst per process.
        self.assertEqual([(k, i) for k, i, _ in self.sent], [("ch", 0), ("ch", 1), ("ch", 0)])
        self.assertTrue(all(len(t) <= MAX_TEXT for _, _, t in self.sent))


if __name__ == "__main__":
    unittest.main()
