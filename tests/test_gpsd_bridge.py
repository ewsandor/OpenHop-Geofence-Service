"""Check the bridge's snapshot publication with a simulated gpspipe."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HANDSHAKE = (
    '{"class":"VERSION","release":"3.25"}\n'
    '{"class":"DEVICES","devices":[]}\n'
    '{"class":"WATCH","nmea":true}\n'
)
NMEA = (
    "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A\n"
    "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\n"
)


class BridgeTests(unittest.TestCase):
    def test_snapshot_publication(self):
        unit = (ROOT / "packaging/gpsd-nmea-bridge.service").read_text()
        command = unit.split("ExecStart=/bin/sh -c '", 1)[1].split("'\n", 1)[0]
        command = command.replace("\\\n", "").replace(
            "while :; do", "for iteration in 1; do", 1
        )
        cases = (
            ("mixed-stream", HANDSHAKE + NMEA, 0, NMEA),
            ("handshake-only", HANDSHAKE, 0, None),
            ("failed-stream", HANDSHAKE + NMEA, 1, None),
            ("empty-stream", "", 0, None),
        )
        for name, stream, exit_code, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                fixture = directory / "stream"
                fixture.write_text(stream)
                gpspipe = directory / "gpspipe"
                gpspipe.write_text(
                    '#!/bin/sh\ncat "$BRIDGE_FIXTURE"\nexit "$BRIDGE_EXIT_CODE"\n'
                )
                gpspipe.chmod(0o755)
                snapshot = directory / "nmea.txt"
                snapshot.write_text("old snapshot")
                result = subprocess.run(
                    ["/bin/sh", "-c", command],
                    env={
                        **os.environ,
                        "PATH": f"{directory}:{os.environ['PATH']}",
                        "RUNTIME_DIRECTORY": str(directory),
                        "BRIDGE_FIXTURE": str(fixture),
                        "BRIDGE_EXIT_CODE": str(exit_code),
                    },
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if expected is None:
                    self.assertFalse(snapshot.exists())
                else:
                    self.assertEqual(snapshot.read_text(), expected)
                self.assertFalse((directory / "nmea.raw").exists())
                self.assertFalse((directory / "nmea.tmp").exists())
