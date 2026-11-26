"""Configuration, clients, persistence, and minimal installs (design tests 3, 6, 8, 10, 17)."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from fakes import FakeClock, FakeGpsdServer, FakeOpenHopServer

from openhop_geofence import config
from openhop_geofence.gpsd import GpsdReader
from openhop_geofence.openhop import OpenHopAuthError, OpenHopClient, OpenHopProtocolError, OpenHopUnavailable
from openhop_geofence.state import (
    SafeModeLatch, SafeModeStore, TxLockout, TxLockoutStore, atomic_write,
)
from openhop_geofence.system import Notifier

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config" / "config.toml.example"


class ConfigTests(unittest.TestCase):
    def test_example_is_valid(self):
        cfg = config.load_config(EXAMPLE)
        self.assertEqual(cfg.web.port, 8080)
        self.assertEqual(cfg.policy.monitor_mode, "monitor")
        self.assertFalse(cfg.meshmapper.enabled)
        self.assertEqual(cfg.meshmapper.lookback_days, 14)

    def test_empty_file_uses_defaults(self):
        cfg = config.parse_config("")
        self.assertFalse(cfg.web.enabled)
        self.assertFalse(cfg.meshmapper.enabled)
        self.assertEqual(cfg.zones, ())
        self.assertTrue(cfg.maintenance.allow_reboot)

    def test_named_zones_need_no_id(self):
        zone = "[[zones]]\nname='Home zone'\nrole='monitor'\nlatitude=0\nlongitude=0\nradius_m=1\n"
        cfg = config.parse_config(zone + zone)
        self.assertEqual([z.name for z in cfg.zones], ["Home zone", "Home zone"])
        self.assertNotIn("id", cfg.zones[0].to_geojson()["properties"])

    def test_rejects_unknown_and_invalid_settings(self):
        for text in (
            "[policy]\nbogus = 1\n",
            "[unknown]\n",
            "[policy]\noutside = 'sometimes'\n",
            "[policy]\nmonitor_mode = 'forward'\n",
            "[web]\nport = 70000\n",
            "[policy]\nrepeat_delay_seconds = -1\n",
            "[[zones]]\nname='a'\nid='a'\nrole='repeat'\nlatitude=0\nlongitude=0\nradius_m=1\n",
            "not toml =",
        ):
            with self.subTest(text=text), self.assertRaises(config.ConfigError):
                config.parse_config(text)

    def test_region_scope_is_admin_only(self):
        old = config.parse_config("[meshmapper]\ncountry='US'\nregions=['ORD']\n")
        new = config.parse_config("[meshmapper]\ncountry='CA'\nregions=['YOW']\n")
        self.assertEqual(config.changed_settings(old, new, config.ADMIN_ONLY_SETTINGS),
                         ["meshmapper.country", "meshmapper.regions"])

    def test_disk_trace_requires_limits(self):
        with self.assertRaises(config.ConfigError):
            config.parse_config("[diagnostics]\nenabled = true\nstorage = 'disk'\n")

    def test_missing_meshmapper_credentials_are_not_an_error(self):
        cfg = config.parse_config("[meshmapper]\nenabled = true\napi_key_file = '/nonexistent'\n")
        self.assertTrue(cfg.meshmapper.enabled)

    def test_meshmapper_regions_and_flat_experimental_settings(self):
        cfg = config.parse_config(
            "[meshmapper]\ncountry='US'\nregions=['ORD', 'TST']\n"
            "weak_coverage_thresholds=true\nmin_snr_db=-10\n"
        )
        self.assertEqual(cfg.meshmapper.regions, ["ORD", "TST"])
        self.assertTrue(cfg.meshmapper.weak_coverage_thresholds)
        self.assertTrue(cfg.warnings)
        for text in (
            "[meshmapper]\nregions=['ORD']\n",
            "[meshmapper]\ncountry='us'\nregions=['ORD']\n",
            "[meshmapper]\ncountry='US'\nregions=['ORD','ORD']\n",
            "[meshmapper]\ncountry='US'\nregions=['../']\n",
            "[meshmapper]\nweak_coverage_thresholds=true\n",
            "[meshmapper.experimental]\nweak_coverage_thresholds=false\n",
            "[meshmapper]\nregion_urls=[]\n",
        ):
            with self.subTest(text=text), self.assertRaises(config.ConfigError):
                config.parse_config(text)


class OpenHopClientTests(unittest.TestCase):
    def test_read_write_and_errors(self):
        with FakeOpenHopServer("monitor", api_key="k") as srv:
            client = OpenHopClient(srv.url, "k", 2)
            state = client.read_state()
            self.assertEqual(state.mode, "monitor")
            self.assertEqual(state.radio.frequency_hz, 906_875_000)
            client.set_mode("forward")
            self.assertEqual(client.read_state().mode, "forward")
            with self.assertRaises(OpenHopAuthError):
                OpenHopClient(srv.url, "wrong", 2).read_state()
            srv.mode = "sleeping"
            with self.assertRaises(OpenHopProtocolError):
                client.read_state()
        with self.assertRaises(OpenHopUnavailable):
            OpenHopClient(srv.url, "k", 0.5).read_state()

    def test_success_false_in_http_200(self):
        with FakeOpenHopServer() as srv:
            client = OpenHopClient(srv.url, "secret", 2)
            with self.assertRaises(ValueError):
                client.set_mode("bogus")  # Rejected locally before sending.
            # Bypass local validation to exercise the server's success:false reply.
            with mock.patch("openhop_geofence.openhop.MODES", ("bogus",)), \
                    self.assertRaisesRegex(OpenHopProtocolError, "rejected"):
                client.set_mode("bogus")


class GpsdTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.trusted = True
        self.reader = GpsdReader("127.0.0.1", 1, 10, clock=self.clock, wall_clock=self.clock.wall,
                                 time_trusted=lambda: self.trusted)

    def test_fix_loss_and_staleness(self):
        self.reader.handle_report({"class": "TPV", "mode": 3, "lat": 40.0, "lon": -105.0})
        self.assertIsNotNone(self.reader.current_fix())
        self.reader.handle_report({"class": "TPV", "mode": 1})
        self.assertIsNone(self.reader.current_fix())  # Explicit no-fix is immediate.
        self.reader.handle_report({"class": "TPV", "mode": 2, "lat": 40.0, "lon": -105.0})
        self.clock.advance(11)
        self.assertIsNone(self.reader.current_fix())

    def test_rejects_invalid_and_replayed_reports(self):
        self.reader.handle_report({"class": "TPV", "mode": 3, "lat": 95.0, "lon": 0})
        self.assertIsNone(self.reader.current_fix())
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock.wall() - 60))
        self.reader.handle_report({"class": "TPV", "mode": 3, "lat": 40, "lon": -105, "time": old})
        self.assertIsNone(self.reader.current_fix())
        self.trusted = False  # Untrusted wall clock: report time is not used.
        self.reader.handle_report({"class": "TPV", "mode": 3, "lat": 40, "lon": -105, "time": old})
        self.assertIsNotNone(self.reader.current_fix())

    def test_socket_session(self):
        with FakeGpsdServer(40.0, -105.0) as srv:
            reader = GpsdReader("127.0.0.1", srv.port, 10)
            reader.start()
            try:
                deadline = time.monotonic() + 5
                while reader.current_fix() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertAlmostEqual(reader.current_fix().lat, 40.0)
            finally:
                reader.stop()


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_atomic_write_preserves_mode(self):
        p = self.dir / "f"
        atomic_write(p, b"one", mode=0o640)
        atomic_write(p, b"two", mode=0o640)
        self.assertEqual(p.read_bytes(), b"two")
        self.assertEqual(p.stat().st_mode & 0o777, 0o640)
        self.assertEqual([x.name for x in self.dir.iterdir()], ["f"])

    def test_corrupt_latch_is_active(self):
        store = SafeModeStore(self.dir)
        self.assertFalse(store.load().active)
        store.save(SafeModeLatch(True, "test", None))
        self.assertTrue(SafeModeStore(self.dir).load().active)
        store.path.write_text("{garbage")
        self.assertTrue(store.load().active)

    def test_operator_tx_lockout_persists_and_corruption_fails_closed(self):
        store = TxLockoutStore(self.dir)
        self.assertFalse(store.load().active)
        store.save(TxLockout(True, "operator request", None))
        self.assertTrue(TxLockoutStore(self.dir).load().active)
        store.path.write_text("{garbage")
        self.assertTrue(store.load().active)


class NotifierTests(unittest.TestCase):
    def test_sd_notify_datagrams(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "notify")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(path)
            old = os.environ.get("NOTIFY_SOCKET")
            os.environ["NOTIFY_SOCKET"] = path
            try:
                n = Notifier()
                n.ready()
                n.watchdog()
                self.assertEqual(sock.recv(64), b"READY=1")
                self.assertEqual(sock.recv(64), b"WATCHDOG=1")
                n._sock.close()
            finally:
                sock.close()
                if old is None:
                    os.environ.pop("NOTIFY_SOCKET")
                else:
                    os.environ["NOTIFY_SOCKET"] = old


class MinimalInstallTests(unittest.TestCase):
    """Simulate missing optional packages in a child interpreter."""

    SCRIPT = r"""
import json, sys
for name in ("shapely", "flask", "waitress", "tomlkit", "meshcore"):
    sys.modules[name] = None
from openhop_geofence import config, zones
from openhop_geofence.web import web_available
out = {"circle": zones.match_zones(config.parse_config(sys.argv[1]).zones, 40, -105).names}
try:
    config.parse_config(sys.argv[2])
except config.ConfigError as exc:
    out["polygon_error"] = str(exc)
out["web"] = web_available()
print(json.dumps(out))
"""

    def test_core_works_without_optional_packages(self):
        circle = "[[zones]]\nname='c'\nrole='repeat'\nlatitude=40\nlongitude=-105\nradius_m=10\n"
        poly = ("[[zones]]\nname='p'\nrole='repeat'\nshape='geojson'\n"
                "geometry={type='Polygon', coordinates=[[[0,0],[1,0],[1,1],[0,0]]]}\n")
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        res = subprocess.run([sys.executable, "-c", self.SCRIPT, circle, poly], env=env,
                             capture_output=True, text=True, check=True)
        out = json.loads(res.stdout)
        self.assertEqual(out["circle"], ["c"])
        self.assertIn("python3-shapely", out["polygon_error"])
        self.assertFalse(out["web"][0])


if __name__ == "__main__":
    unittest.main()
