"""End-to-end process tests with fake OpenHop and gpsd servers (design tests 1, 9, 10)."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fakes import FakeGpsdServer, FakeOpenHopServer

ROOT = Path(__file__).resolve().parent.parent

CONFIG = """\
[openhop]
base_url = "{url}"
api_key_file = "{key}"

[gpsd]
host = "127.0.0.1"
port = {gps_port}

[policy]
outside = "monitor"
repeat_delay_seconds = 1
minimum_monitor_seconds = 1

[meshmapper]
enabled = false

[[zones]]
id = "trail"
role = "repeat"
latitude = 40.0
longitude = -105.0
radius_m = 1000
"""


class ProcessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "key").write_text("secret\n")
        self.env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("NOTIFY_SOCKET", None)

    def command(self, *args):
        return [sys.executable, "-m", "openhop_geofence", "--config", str(self.dir / "config.toml"),
                "--state-dir", str(self.dir / "state"), "--cache-dir", str(self.dir / "cache"),
                "--runtime-dir", str(self.dir / "run"), *args]

    def write_config(self, oh, gps):
        (self.dir / "config.toml").write_text(
            CONFIG.format(url=oh.url, key=self.dir / "key", gps_port=gps.port))

    def wait_for(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def test_switches_to_repeat_in_zone_and_stops_cleanly(self):
        with FakeOpenHopServer("monitor") as oh, FakeGpsdServer(40.0, -105.0) as gps:
            self.write_config(oh, gps)
            proc = subprocess.Popen(self.command(), env=self.env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            try:
                self.assertTrue(self.wait_for(lambda: oh.mode == "forward"), "never switched to forward")
                proc.send_signal(signal.SIGTERM)
                out, _ = proc.communicate(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
        self.assertEqual(proc.returncode, 0, out)
        # A clean steady-state run writes nothing to persistent storage: no
        # safe mode latch, and no state or cache files (design test 9).
        for d in ("state", "cache"):
            files = [p for p in (self.dir / d).rglob("*") if p.is_file()] if (self.dir / d).exists() else []
            self.assertEqual(files, [], d)

    def test_check_config(self):
        with FakeOpenHopServer() as oh, FakeGpsdServer(0, 0) as gps:
            self.write_config(oh, gps)
        ok = subprocess.run(self.command("--check-config"), env=self.env, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        (self.dir / "config.toml").write_text("[policy]\noutside = 'sometimes'\n")
        bad = subprocess.run(self.command("--check-config"), env=self.env, capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("outside", bad.stderr)

    def test_invalid_config_at_startup_disables_tx(self):
        with FakeOpenHopServer("forward") as oh:
            (self.dir / "config.toml").write_text(
                f'[openhop]\nbase_url = "{oh.url}"\napi_key_file = "{self.dir / "key"}"\n'
                "[policy]\noutside = 'sometimes'\n")
            res = subprocess.run(self.command(), env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual(res.returncode, 78, res.stderr)
            self.assertEqual(oh.mode, "no_tx")
        self.assertTrue((self.dir / "state" / "safe-mode.json").exists())


if __name__ == "__main__":
    unittest.main()
