"""Zones, decision priority, and hysteresis (design tests 1, 2, 4)."""

import unittest

from openhop_geofence import config, policy, zones
from openhop_geofence.policy import CoverageResult, Hysteresis, decide

try:
    import shapely  # noqa: F401

    HAVE_SHAPELY = True
except ImportError:
    HAVE_SHAPELY = False


def circle(name, role, lat, lon, r):
    return config.parse_zone({"name": name, "role": role, "latitude": lat, "longitude": lon, "radius_m": r})


class ZoneTests(unittest.TestCase):
    def test_overlap_reports_both_roles(self):
        zs = [circle("rep", "repeat", 40.0, -105.0, 1000), circle("mon", "monitor", 40.0, -105.0, 100)]
        m = zones.match_zones(zs, 40.0, -105.0)
        self.assertEqual(list(m.monitor), ["mon"])
        self.assertEqual(list(m.repeat), ["rep"])
        m = zones.match_zones(zs, 40.005, -105.0)  # ~556 m north: repeat only
        self.assertEqual(list(m.monitor), [])
        self.assertEqual(list(m.repeat), ["rep"])

    def test_circle_boundary_uses_haversine(self):
        z = circle("c", "repeat", 0.0, 0.0, 1000)
        # 1 km is ~0.008993 degrees of latitude.
        self.assertTrue(z.contains(0.00899, 0.0))
        self.assertFalse(z.contains(0.00901, 0.0))

    def test_invalid_zones_rejected(self):
        bad = [
            {"name": "x", "role": "repeat", "latitude": 91, "longitude": 0, "radius_m": 10},
            {"name": "x", "role": "repeat", "latitude": 0, "longitude": 0, "radius_m": 0},
            {"name": "x", "role": "other", "latitude": 0, "longitude": 0, "radius_m": 10},
            {"role": "repeat", "latitude": 0, "longitude": 0, "radius_m": 10},
            {"name": " ", "role": "repeat", "latitude": 0, "longitude": 0, "radius_m": 10},
            {"name": "x", "role": "repeat", "latitude": 0, "longitude": 0, "radius_m": 10, "extra": 1},
            {"name": "x", "role": "repeat", "latitude": float("nan"), "longitude": 0, "radius_m": 10},
        ]
        for z in bad:
            with self.subTest(z=z), self.assertRaises(ValueError):
                config.parse_zone(z)

    @unittest.skipUnless(HAVE_SHAPELY, "Shapely not installed")
    def test_polygon_with_hole(self):
        outer = [[0, 0], [0.1, 0], [0.1, 0.1], [0, 0.1], [0, 0]]
        hole = [[0.04, 0.04], [0.06, 0.04], [0.06, 0.06], [0.04, 0.06], [0.04, 0.04]]
        z = config.parse_zone({"name": "p", "role": "monitor", "shape": "geojson",
                               "geometry": {"type": "Polygon", "coordinates": [outer, hole]}})
        self.assertTrue(z.contains(0.02, 0.02))
        self.assertFalse(z.contains(0.05, 0.05))
        self.assertTrue(z.contains(0.0, 0.05))  # Boundary is inside.

    @unittest.skipUnless(HAVE_SHAPELY, "Shapely not installed")
    def test_self_intersecting_polygon_rejected(self):
        bowtie = [[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]
        with self.assertRaises(ValueError):
            config.parse_zone({"name": "p", "role": "monitor", "shape": "geojson",
                               "geometry": {"type": "Polygon", "coordinates": [bowtie]}})


def zm(monitor=(), repeat=()):
    return zones.ZoneMatch(tuple(monitor), tuple(repeat))


def run(**kw):
    args = dict(safe_mode=False, override=None, fix_valid=True, zone_match=zm(), coverage=None,
                outside="monitor", held=None, monitor_mode="monitor")
    args.update(kw)
    return decide(**args)


class DecisionTests(unittest.TestCase):
    def test_priority_order(self):
        covered = CoverageResult(True, policy.COVERED)
        self.assertEqual(run(safe_mode=True, override="forward").openhop_mode, "no_tx")
        self.assertEqual(run(override="forward", fix_valid=False).openhop_mode, "forward")
        self.assertEqual(run(fix_valid=False, zone_match=None, outside="repeat").source, policy.SRC_GPS)
        self.assertEqual(run(zone_match=zm(["m"], ["r"])).source, policy.SRC_MONITOR_ZONE)
        self.assertEqual(run(zone_match=zm([], ["r"]), coverage=covered).source, policy.SRC_REPEAT_ZONE)
        self.assertEqual(run(coverage=covered, outside="repeat").source, policy.SRC_MESHMAPPER)

    def test_meshmapper_classes(self):
        self.assertEqual(run(coverage=CoverageResult(True, policy.COVERED)).result, policy.MONITOR)
        self.assertEqual(run(coverage=CoverageResult(True, policy.UNCOVERED)).result, policy.REPEAT)
        self.assertEqual(run(coverage=CoverageResult(True, policy.WEAK)).result, policy.REPEAT)

    def test_outside_policies_and_fallback(self):
        unavailable = CoverageResult(False, reason="offline")
        for cov in (None, unavailable):
            with self.subTest(coverage=cov):
                self.assertEqual(run(coverage=cov, outside="monitor").result, policy.MONITOR)
                self.assertEqual(run(coverage=cov, outside="repeat").result, policy.REPEAT)
                self.assertEqual(run(coverage=cov, outside="last", held=policy.REPEAT).result, policy.REPEAT)
                self.assertEqual(run(coverage=cov, outside="last", held=None).result, policy.MONITOR)

    def test_monitor_mode_mapping(self):
        self.assertEqual(run(monitor_mode="no_tx").openhop_mode, "no_tx")
        self.assertEqual(run(outside="repeat", monitor_mode="no_tx").openhop_mode, "forward")
        # Force Monitor is the OpenHop monitor mode regardless of mapping.
        self.assertEqual(run(override="monitor", monitor_mode="no_tx").openhop_mode, "monitor")


class HysteresisTests(unittest.TestCase):
    def test_repeat_requires_continuous_eligibility(self):
        h = Hysteresis(10, 30, now=0)
        rep, mon = run(outside="repeat"), run()
        self.assertEqual(h.apply(rep, 0, "monitor")[0], "monitor")
        self.assertEqual(h.apply(rep, 29, "monitor")[0], "monitor")  # minimum Monitor time
        self.assertEqual(h.apply(rep, 30, "monitor")[0], "forward")
        # Leaving is immediate; jitter back restarts both timers.
        self.assertEqual(h.apply(mon, 31, "monitor")[0], "monitor")
        self.assertEqual(h.apply(rep, 32, "monitor"), ("monitor", 29))
        self.assertEqual(h.apply(mon, 33, "monitor")[0], "monitor")
        self.assertEqual(h.apply(rep, 50, "monitor")[0], "monitor")
        self.assertEqual(h.apply(rep, 61, "monitor")[0], "forward")

    def test_pending_repeat_uses_monitor_mapping(self):
        h = Hysteresis(10, 0, now=0)
        self.assertEqual(h.apply(run(outside="repeat", monitor_mode="no_tx"), 0, "no_tx")[0], "no_tx")

    def test_overrides_bypass_timers(self):
        h = Hysteresis(10, 30, now=0)
        self.assertEqual(h.apply(run(override="forward"), 0, "monitor")[0], "forward")


if __name__ == "__main__":
    unittest.main()
