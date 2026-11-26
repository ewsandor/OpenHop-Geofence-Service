"""Diagnostics trace recorder and offline converter (design test 16)."""

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from fakes import FakeClock

from openhop_geofence import policy, trace_convert
from openhop_geofence.config import DiagnosticsConfig
from openhop_geofence.controller import Evaluation
from openhop_geofence.gpsd import Fix
from openhop_geofence.trace import TraceRecorder

REPEAT = policy.Decision("zone", "repeat", "forward", "repeat zone", True)


def evaluation(clock, lat, mode="forward", decision=REPEAT, fix=True):
    return Evaluation(
        fix=Fix(lat, -105.0, 3, clock()) if fix else None, decision=decision, desired_mode=mode,
        observed_mode=mode, observed_age=1.0, request_mode=None, request_error=None,
        zone_names=("z",), coverage=None,
    )


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def recorder(self, **kw):
        cfg = DiagnosticsConfig(enabled=True, sample_interval_seconds=5, **kw)
        return TraceRecorder(cfg, clock=self.clock, wall_clock=self.clock.wall, time_trusted=lambda: True)

    def test_samples_at_interval_and_transitions_immediately(self):
        rec = self.recorder()
        for i in range(10):
            rec.on_evaluation(evaluation(self.clock, 40 + i * 1e-4))
            self.clock.advance(1)
        monitor = policy.Decision("zone", "monitor", "monitor", "monitor zone", True)
        rec.on_evaluation(evaluation(self.clock, 40.01, "monitor", monitor))
        records, _ = rec.snapshot()
        self.assertEqual([r["type"] for r in records], ["transition", "sample", "transition"])
        self.assertEqual(records[-1]["confirmed"], "monitor")

    def test_ram_buffer_is_bounded(self):
        rec = self.recorder(max_buffer_bytes=2000)
        for i in range(100):
            rec.on_evaluation(evaluation(self.clock, 40 + i * 1e-4))
            self.clock.advance(5)
        st = rec.status()
        self.assertLessEqual(st["buffer_bytes"], 2000)
        self.assertGreater(st["dropped_records"], 0)

    def test_disabled_recorder_records_nothing(self):
        rec = TraceRecorder(DiagnosticsConfig(enabled=False), clock=self.clock)
        rec.on_evaluation(evaluation(self.clock, 40))
        self.assertEqual(rec.snapshot(), ([], []))
        with self.assertRaises(RuntimeError):
            rec.set_recording(True)

    def test_disk_storage_and_export(self):
        with tempfile.TemporaryDirectory() as d:
            rec = self.recorder(storage="disk", directory=d, max_disk_bytes=1 << 20, max_age_seconds=86400)
            for i in range(3):
                rec.on_evaluation(evaluation(self.clock, 40 + i * 1e-3))
                self.clock.advance(5)
            rec.stop()  # Flushes the writer.
            self.assertTrue(list(Path(d).glob("records-*.jsonl")))
            with rec.export_zip() as fh, zipfile.ZipFile(fh) as zf:
                self.assertEqual(len(zf.read("records.jsonl").splitlines()), 3)
                self.assertEqual(json.loads(zf.read("manifest.json"))["format"], "openhop-geofence-trace")


class ConverterTests(unittest.TestCase):
    def record(self, elapsed, lat, mode, kind="sample", session="s1"):
        return {"type": kind, "session": session, "elapsed": elapsed, "fix": True,
                "lat": lat, "lon": -105.0, "confirmed": mode}

    def test_segments_split_by_mode_gap_and_fix_loss(self):
        records = [
            self.record(0, 40.0, "forward", "transition"),
            self.record(5, 40.1, "forward"),
            self.record(10, 40.2, "monitor", "transition"),
            self.record(15, 40.3, "monitor"),
            self.record(500, 40.4, "monitor"),  # Gap: new segment.
            self.record(505, 40.5, None),  # Unknown confirmed mode.
            {"type": "sample", "session": "s1", "elapsed": 510, "fix": False},
            {"type": "event", "session": "s1", "level": "warning", "kind": "gps", "message": "lost"},
        ]
        context = [{"type": "square", "bounds": [40, -105, 40.01, -104.99], "grid_id": "g"}]
        gj = trace_convert.to_geojson(records, context)
        tracks = [f for f in gj["features"] if f["properties"]["layer"] == "track"]
        self.assertEqual([(t["properties"]["mode"], t["properties"]["points"]) for t in tracks],
                         [("forward", 2), ("monitor", 3), ("unknown", 2)])
        layers = [f["properties"]["layer"] for f in gj["features"]]
        self.assertEqual(layers.count("transition"), 2)
        self.assertEqual(layers.count("event"), 1)
        self.assertEqual(layers[0], "square")

    def test_cli_reads_export_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("records.jsonl", "\n".join(json.dumps(self.record(i, 40 + i / 100, "forward"))
                                                   for i in range(3)) + "\n{bad json\n")
            zf.writestr("context.jsonl", "")
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "t.zip", Path(d) / "t.geojson"
            src.write_bytes(buf.getvalue())
            self.assertEqual(trace_convert.main([str(src), "-o", str(out)]), 0)
            gj = json.loads(out.read_text())
        self.assertEqual(gj["features"][0]["geometry"]["type"], "LineString")


if __name__ == "__main__":
    unittest.main()
