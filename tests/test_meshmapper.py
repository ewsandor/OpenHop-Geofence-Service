"""MeshMapper parsing, lookup, caching, and rate limits (design tests 7, 18)."""

import json
import tempfile
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path

from fakes import FakeClock

from openhop_geofence import config, policy
from openhop_geofence.meshmapper import (
    ACCOUNTING_FILE,
    MeshMapperError,
    MeshMapperWorker,
    _SameOriginRedirect,
    _error_for_status,
    normalize_timestamp,
    parse_boundaries,
    parse_coverage,
    radio_filter,
)
from openhop_geofence.openhop import RadioSettings
from openhop_geofence.state import RuntimeDir

try:
    import shapely  # noqa: F401

    HAVE_SHAPELY = True
except ImportError:
    HAVE_SHAPELY = False

RADIO = RadioSettings(906_875_000, 250_000, 10, 5, False)
BOUNDARY = {"type": "FeatureCollection", "features": [{
    "type": "Feature", "properties": {"code": "TST"},
    "geometry": {"type": "Polygon", "coordinates": [[[-106, 39], [-104, 39], [-104, 41], [-106, 41], [-106, 39]]]},
}]}


def square(s, w, ctype="BIDIR", ts=None, snr=None, effective=None, gid="g"):
    return {"grid_id": gid, "bounds": {"south": s, "west": w, "north": s + 0.0027, "east": w + 0.0038},
            "coverage_type": ctype, "timestamp": ts, "snr": snr, "effective": effective}


class FakeApi:
    """Records requests and returns queued (status, body, headers) responses."""

    def __init__(self, clock, squares):
        self.clock = clock
        self.squares = squares
        self.calls = []
        self.coverage_status = 200
        self.directory = {"country": "US", "zones": [
            {"code": "TST", "url": "https://test.meshmapper.net/"},
            {"code": "TWO", "url": "https://two.meshmapper.net/"},
        ]}

    def __call__(self, url, key, etag=None):
        self.calls.append((url, etag))
        if "get_zones" in url:
            return 200, json.dumps(self.directory).encode(), {}
        if "get_geojson" in url:
            boundary = json.loads(json.dumps(BOUNDARY))
            if "two.meshmapper.net" in url:
                boundary["features"][0]["properties"]["code"] = "TWO"
            return 200, json.dumps(boundary).encode(), {}
        if self.coverage_status != 200:
            return self.coverage_status, json.dumps({"error": "rate_limited"}).encode(), {}
        if etag:
            return 304, b"", {}
        body = {"success": True, "generated_at": self.clock.wall(), "grid_squares": self.squares,
                "filters": {"f_radio_freq": "906.875,250,10,5"}}
        return 200, json.dumps(body).encode(), {"etag": '"v1"'}


class ParsingTests(unittest.TestCase):
    def test_radio_filter_units(self):
        self.assertEqual(radio_filter(RADIO), "906.875,250,10,5")
        self.assertEqual(radio_filter(RadioSettings(869_525_000, 62_500, 8, 8, False)), "869.525,62.5,8,8")

    def test_timestamps(self):
        self.assertEqual(normalize_timestamp(1_700_000_000_000), 1_700_000_000)
        self.assertEqual(normalize_timestamp(1_700_000_000), 1_700_000_000)
        self.assertIsNone(normalize_timestamp(None))
        self.assertIsNone(normalize_timestamp(True))

    def test_rejects_partial_or_unknown_data(self):
        good = {"success": True, "generated_at": 1_700_000_000, "grid_squares": [square(40, -105)]}
        self.assertEqual(len(parse_coverage(good)[0]), 1)
        for bad in (
            {**good, "success": False},
            {**good, "generated_at": None},
            {**good, "grid_squares": [square(40, -105, ctype="NEW")]},
            {**good, "grid_squares": [{"bounds": {"south": 41, "west": 0, "north": 40, "east": 1},
                                       "coverage_type": "BIDIR"}]},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_coverage(bad)

    @unittest.skipUnless(HAVE_SHAPELY, "Shapely not installed")
    def test_null_boundary_is_unknown_scope(self):
        with self.assertRaisesRegex(ValueError, "unknown scope"):
            parse_boundaries({"type": "FeatureCollection",
                              "features": [{"properties": {"code": "X"}, "geometry": None}]})

    def test_authenticated_redirect_cannot_leave_origin(self):
        handler = _SameOriginRedirect()
        req = urllib.request.Request("https://meshmapper.net/get_zones.php")
        with self.assertRaises(MeshMapperError):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/")

    def test_api_not_allowed_explains_required_permissions(self):
        error = _error_for_status(403, b'{"error":"api_not_allowed"}', {})
        self.assertTrue(error.config_fault)
        self.assertIn("requested API", str(error))
        self.assertIn("region- or group-scoped", str(error))
        self.assertIn("integration key", str(error))
        self.assertNotIn("cannot access Coverage", str(error))


@unittest.skipUnless(HAVE_SHAPELY, "Shapely not installed")
class WorkerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "key").write_text("test-key\n")
        self.clock = FakeClock()
        self.trusted = True
        self.cfg = config.MeshMapperConfig(
            enabled=True, api_key_file=str(self.dir / "key"),
            country="US", regions=["TST"],
        )
        now = self.clock.wall()
        self.api = FakeApi(self.clock, [
            square(40.0, -105.0, "BIDIR", ts=now - 3600, snr=-12, effective=1, gid="covered"),
            square(40.01, -105.0, "DEAD", ts=now - 3600, gid="dead"),
            square(40.02, -105.0, "BIDIR", ts=now - 30 * 86400, gid="old"),
        ])
        self.runtime = RuntimeDir(self.dir / "run")

    def worker(self, cfg=None):
        return MeshMapperWorker(cfg or self.cfg, self.dir / "cache", self.runtime, lambda: RADIO,
                                lambda: self.trusted, wall_clock=self.clock.wall, http_get=self.api)

    def initial_refresh(self, worker, regions=1):
        for _ in range(regions + 2):
            worker.refresh_once()
            self.clock.advance(60)

    def test_lookup_classes_and_scope(self):
        w = self.worker()
        self.initial_refresh(w)
        self.assertEqual(w.lookup(40.001, -104.998), policy.CoverageResult(True, policy.COVERED, "BIDIR", "covered"))
        self.assertEqual(w.lookup(40.011, -104.998).classification, policy.UNCOVERED)
        self.assertEqual(w.lookup(40.021, -104.998).classification, policy.UNCOVERED)  # older than 14 days
        self.assertEqual(w.lookup(40.5, -104.5).classification, policy.UNCOVERED)  # no square, in scope
        out = w.lookup(45.0, -104.5)
        self.assertFalse(out.available)
        self.assertIn("outside", out.reason)
        self.assertIn("f_radio_freq=906.875%2C250%2C10%2C5", self.api.calls[-1][0])
        self.assertIn("f_days=14", self.api.calls[-1][0])

    def test_experimental_thresholds(self):
        exp = replace(self.cfg, weak_coverage_thresholds=True, min_snr_db=-10.0)
        w = self.worker(exp)
        self.initial_refresh(w)
        self.assertEqual(w.lookup(40.001, -104.998).classification, policy.WEAK)
        # Thresholds are ignored while the experimental switch is off.
        off = replace(self.cfg, weak_coverage_thresholds=False, min_snr_db=-10.0)
        w = self.worker(off)
        w._load_snapshot()
        self.assertEqual(w.lookup(40.001, -104.998).classification, policy.COVERED)

    def test_snapshot_reused_after_restart_without_refetch(self):
        w = self.worker()
        self.initial_refresh(w)
        calls = len(self.api.calls)
        snapshot = (self.dir / "cache" / "coverage.json.gz").stat().st_mtime_ns
        self.clock.advance(120)
        w2 = self.worker()
        w2._load_snapshot()
        w2.refresh_once()
        self.assertEqual(len(self.api.calls), calls)
        self.assertTrue(w2.lookup(40.001, -104.998).available)
        self.assertEqual((self.dir / "cache" / "coverage.json.gz").stat().st_mtime_ns, snapshot)

    def test_snapshot_scope_includes_country_and_regions(self):
        w = self.worker()
        self.initial_refresh(w)
        for cfg in (replace(self.cfg, country="CA"), replace(self.cfg, regions=["TWO"])):
            other = self.worker(cfg)
            other._load_snapshot()
            self.assertFalse(other.lookup(40.001, -104.998).available)

    def test_offline_cache_expires(self):
        w = self.worker()
        self.initial_refresh(w)
        self.clock.advance(self.cfg.max_cache_age_seconds + 1)
        self.api.coverage_status = 503
        w.refresh_once()
        self.assertFalse(w.lookup(40.001, -104.998).available)

    def test_untrusted_time_disables_requests_and_cache(self):
        self.trusted = False
        w = self.worker()
        w.refresh_once()
        self.assertEqual(self.api.calls, [])
        self.assertFalse(w.lookup(40.001, -104.998).available)

    def test_backoff_survives_worker_restart(self):
        self.api.coverage_status = 429
        w = self.worker()
        self.initial_refresh(w)
        calls = len(self.api.calls)
        self.clock.advance(60)
        self.worker().refresh_once()  # Fresh worker shares the runtime accounting.
        self.assertEqual(len(self.api.calls), calls)
        acct = self.runtime.read_json(ACCOUNTING_FILE)
        self.assertGreater(acct["backoff_until_utc"], self.clock.wall())

    def test_daily_budget(self):
        w = self.worker(replace(self.cfg, daily_request_budget=2, refresh_seconds=900))
        self.initial_refresh(w)
        self.clock.advance(1000)
        self.initial_refresh(w)
        self.assertEqual(len(self.api.calls), 2)

    def test_not_modified_keeps_generated_at(self):
        w = self.worker(replace(self.cfg, refresh_seconds=900))
        self.initial_refresh(w)
        generated = w.status().generated_at
        self.clock.advance(1000)
        w.refresh_once()
        self.assertEqual(self.api.calls[-1][1], '"v1"')
        st = w.status()
        self.assertEqual(st.generated_at, generated)
        self.assertGreater(st.fetched_at, generated)

    def test_client_error_is_configuration_fault(self):
        self.api.coverage_status = 400
        w = self.worker()
        self.initial_refresh(w)
        self.clock.advance(100_000)
        w.refresh_once()
        self.assertEqual(len(self.api.calls), 3)  # No retry until reload.
        self.assertFalse(w.status().available)

    def test_multi_radio_requires_explicit_filter(self):
        w = MeshMapperWorker(self.cfg, self.dir / "cache", self.runtime,
                             lambda: replace(RADIO, multi_radio=True), lambda: True,
                             wall_clock=self.clock.wall, http_get=self.api)
        w.refresh_once()
        self.assertEqual(self.api.calls, [])
        self.assertIn("multi-radio", w.status().reason)

    def test_multiple_regions_and_per_request_spacing(self):
        w = self.worker(replace(self.cfg, regions=["TST", "TWO"]))
        w.refresh_once()
        w.refresh_once()
        self.assertEqual(len(self.api.calls), 1)
        for _ in range(3):
            self.clock.advance(60)
            w.refresh_once()
        self.assertEqual(len(self.api.calls), 4)
        self.assertIn("country=US", self.api.calls[0][0])
        self.assertTrue(w.lookup(40.001, -104.998).available)

    def test_directory_missing_region_or_untrusted_url(self):
        for index, directory in enumerate((
            {"country": "US", "zones": []},
            {"country": "US", "zones": [{"code": "TST", "url": "https://evil.example/"}]},
            {"country": "US", "zones": [{"code": "TST", "url": "https://[invalid/"}]},
        )):
            with self.subTest(directory=directory):
                self.api.directory = directory
                self.clock.advance(60)
                w = self.worker()
                w.runtime = RuntimeDir(self.dir / f"run-{index}")
                w.refresh_once()
                self.assertFalse(w.lookup(40, -105).available)
                self.assertIsNotNone(w.status().last_error)

    def test_boundary_must_match_requested_region(self):
        self.api.directory["zones"][0]["url"] = "https://two.meshmapper.net/"
        w = self.worker()
        self.initial_refresh(w)
        self.assertFalse(w.lookup(40, -105).available)
        self.assertIn("matching region", w.status().last_error)

    def test_revoked_region_stops_using_cached_coverage(self):
        w = self.worker()
        self.initial_refresh(w)
        self.api.directory["zones"] = []
        self.clock.advance(self.cfg.boundary_refresh_seconds)
        w.refresh_once()
        self.assertIn("not authorized", w.status().last_error)
        self.assertFalse(w.lookup(40.001, -104.998).available)

    def test_completed_boundary_refresh_does_not_need_extra_budget(self):
        w = self.worker(replace(self.cfg, daily_request_budget=5, refresh_seconds=100_000,
                                max_cache_age_seconds=200_000, boundary_refresh_seconds=3600))
        self.initial_refresh(w)
        previous = w._dataset.boundaries_fetched_at
        self.clock.advance(3600)
        w.refresh_once()
        self.clock.advance(60)
        w.refresh_once()
        w.refresh_once()
        self.assertGreater(w._dataset.boundaries_fetched_at, previous)
        self.assertEqual(len(self.api.calls), 5)


if __name__ == "__main__":
    unittest.main()
