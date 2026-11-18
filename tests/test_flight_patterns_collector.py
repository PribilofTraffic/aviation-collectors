#!/usr/bin/env python3
"""Unit tests for flight_patterns_collector pure functions (no network)."""

import os
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import flight_patterns_collector as fpc

# The real AER66 detection recorded 2026-07-28, used as the reference fixture.
AER66_GRID = {
    "callsign": "AER66",
    "center_lat": 56.937924,
    "center_lon": -170.272972,
    "coverage_area": 52.66,
    "detected_at": "2026-07-28T19:51:38.117721",
    "hex_id": "a03000",
    "last_seen": "2026-07-28T19:52:38.117721",
    "max_altitude": 4000,
    "min_altitude": None,
    "num_legs": 3,
    "pattern_type": "racetrack",
    "tar1090_url": "http://ultrafeeder:80/?icao=a03000&lat=56.9379&lon=-170.2730&zoom=13",
}


class TestGeodesy(unittest.TestCase):
    def test_distance_and_bearing_from_st_paul(self):
        dist, bearing = fpc.great_circle(57.1553, -170.2225, 56.937924, -170.272972)
        self.assertAlmostEqual(dist, 13.2, delta=0.2)
        self.assertAlmostEqual(bearing, 187, delta=2)

    def test_nearest_island_picks_st_paul(self):
        name, dist, bearing = fpc.nearest_island(56.937924, -170.272972)
        self.assertEqual(name, "St. Paul")
        self.assertAlmostEqual(dist, 13.2, delta=0.2)
        self.assertEqual(bearing, 187)

    def test_nearest_island_picks_st_george(self):
        # A point 5 NM due east of PAPB.
        name, dist, _ = fpc.nearest_island(56.5783, -169.5119)
        self.assertEqual(name, "St. George")
        self.assertAlmostEqual(dist, 5.0, delta=0.3)


class TestRadarUrl(unittest.TestCase):
    def test_rewrites_internal_hostname(self):
        url = fpc.rewrite_radar_url("a03000", 56.937924, -170.272972)
        self.assertEqual(
            url, "/papb1090/?icao=a03000&lat=56.9379&lon=-170.2730&zoom=13"
        )

    def test_never_leaks_ultrafeeder(self):
        url = fpc.rewrite_radar_url("abc123", 57.0, -170.0)
        self.assertNotIn("ultrafeeder", url)
        self.assertTrue(url.startswith("/papb1090/"))

    def test_rejects_malformed_hex_id_with_markup(self):
        with self.assertRaises(ValueError):
            fpc.rewrite_radar_url("a03000<script>", 57.0, -170.0)

    def test_rejects_empty_hex_id(self):
        with self.assertRaises(ValueError):
            fpc.rewrite_radar_url("", 57.0, -170.0)


class TestAsNumber(unittest.TestCase):
    def test_passes_through_valid_float(self):
        self.assertEqual(fpc._as_number(52.66), 52.66)

    def test_coerces_numeric_string(self):
        self.assertEqual(fpc._as_number("3", kind=int), 3)

    def test_none_stays_none(self):
        self.assertIsNone(fpc._as_number(None))

    def test_malformed_string_becomes_none(self):
        # A one-off bad value from the container must degrade to None, not
        # raise -- this is what keeps a single bad record from taking down
        # the whole detections list downstream.
        self.assertIsNone(fpc._as_number("not-a-number"))
        self.assertIsNone(fpc._as_number("three", kind=int))

    def test_dict_or_list_becomes_none(self):
        self.assertIsNone(fpc._as_number({"nested": 1}))
        self.assertIsNone(fpc._as_number([1, 2, 3]))


class TestNormalize(unittest.TestCase):
    def test_normalizes_grid_record(self):
        det = fpc.normalize_detection(AER66_GRID, "grid")
        self.assertEqual(det["pattern_type"], "racetrack")
        self.assertEqual(det["callsign"], "AER66")
        self.assertEqual(det["hex_id"], "a03000")
        self.assertEqual(det["nearest_island"], "St. Paul")
        self.assertEqual(det["bearing_deg"], 187)
        self.assertAlmostEqual(det["distance_nm"], 13.2, delta=0.2)
        self.assertEqual(det["duration_min"], 1)
        self.assertEqual(det["radar_url"], "/papb1090/?icao=a03000&lat=56.9379&lon=-170.2730&zoom=13")

    def test_uses_fallback_type_when_absent(self):
        raw = dict(AER66_GRID)
        del raw["pattern_type"]
        det = fpc.normalize_detection(raw, "circle")
        self.assertEqual(det["pattern_type"], "circle")

    def test_missing_optional_fields_do_not_raise(self):
        raw = {
            "callsign": "N123AB",
            "center_lat": 57.0,
            "center_lon": -170.0,
            "detected_at": "2026-08-01T10:00:00",
            "hex_id": "aaaaaa",
        }
        det = fpc.normalize_detection(raw, "circle")
        self.assertIsNone(det["max_altitude"])
        self.assertIsNone(det["coverage_area"])
        self.assertEqual(det["duration_min"], 0)

    def test_rejects_record_without_coordinates(self):
        raw = {"callsign": "N123AB", "hex_id": "aaaaaa", "detected_at": "2026-08-01T10:00:00"}
        self.assertIsNone(fpc.normalize_detection(raw, "circle"))

    def test_rejects_record_with_malformed_hex_id(self):
        raw = dict(AER66_GRID, hex_id="a03000<script>")
        self.assertIsNone(fpc.normalize_detection(raw, "grid"))

    def test_still_normalizes_valid_aer66_fixture(self):
        det = fpc.normalize_detection(AER66_GRID, "grid")
        self.assertIsNotNone(det)
        self.assertEqual(det["callsign"], "AER66")
        self.assertEqual(det["hex_id"], "a03000")

    def test_malformed_numeric_fields_degrade_to_none_not_raise(self):
        # The container validates none of these fields. A one-off bad type
        # here (e.g. a string where a number is expected) must not raise and
        # kill normalization of an otherwise-good record.
        raw = dict(
            AER66_GRID,
            coverage_area="unknown",
            num_legs="several",
            max_altitude="FL400",
            min_altitude={"weird": "shape"},
        )
        det = fpc.normalize_detection(raw, "grid")
        self.assertIsNotNone(det)
        self.assertIsNone(det["coverage_area"])
        self.assertIsNone(det["num_legs"])
        self.assertIsNone(det["max_altitude"])
        self.assertIsNone(det["min_altitude"])

    def test_numeric_string_fields_coerce_to_numbers(self):
        raw = dict(AER66_GRID, coverage_area="52.66", num_legs="3", max_altitude="4000")
        det = fpc.normalize_detection(raw, "grid")
        self.assertEqual(det["coverage_area"], 52.66)
        self.assertEqual(det["num_legs"], 3)
        self.assertEqual(det["max_altitude"], 4000)

    def test_distance_nm_is_always_numeric(self):
        det = fpc.normalize_detection(AER66_GRID, "grid")
        self.assertIsInstance(det["distance_nm"], float)


class TestMerge(unittest.TestCase):
    def test_key_combines_hex_time_and_type(self):
        det = fpc.normalize_detection(AER66_GRID, "grid")
        self.assertEqual(
            fpc.detection_key(det), "a03000|2026-07-28T19:51:38.117721-08:00|racetrack"
        )

    def test_merge_deduplicates_repeat_polls(self):
        det = fpc.normalize_detection(AER66_GRID, "grid")
        merged = fpc.merge_detections([det], [det])
        self.assertEqual(len(merged), 1)

    def test_merge_appends_new_detection(self):
        old = fpc.normalize_detection(AER66_GRID, "grid")
        newer_raw = dict(AER66_GRID, detected_at="2026-08-02T01:00:00", hex_id="b11111")
        new = fpc.normalize_detection(newer_raw, "grid")
        merged = fpc.merge_detections([old], [new])
        self.assertEqual(len(merged), 2)

    def test_merge_sorts_newest_first(self):
        old = fpc.normalize_detection(AER66_GRID, "grid")
        newer_raw = dict(AER66_GRID, detected_at="2026-08-02T01:00:00", hex_id="b11111")
        new = fpc.normalize_detection(newer_raw, "grid")
        merged = fpc.merge_detections([old], [new])
        self.assertEqual(merged[0]["hex_id"], "b11111")

    def test_merge_never_drops_archived_detections(self):
        """The container's history can be wiped; ours must not shrink."""
        old = fpc.normalize_detection(AER66_GRID, "grid")
        merged = fpc.merge_detections([old], [])
        self.assertEqual(len(merged), 1)


class TestContainerClock(unittest.TestCase):
    """The container's clock is Alaska local and its timestamps carry no
    offset (2026-08-20's AER66 record is stamped 18:55, the minute our own
    DB has it 8 NM from St. George -- at 18:55 UTC it was still in
    Anchorage). The archive stamps the offset so the page cannot guess."""

    def test_naive_container_time_gets_alaska_offset(self):
        det = fpc.normalize_detection(AER66_GRID, "grid")
        self.assertEqual(det["detected_at"], "2026-07-28T19:51:38.117721-08:00")
        self.assertTrue(det["last_seen"].endswith("-08:00"))

    def test_winter_offset(self):
        self.assertEqual(fpc.alaska_iso("2026-01-10T08:00:00"),
                         "2026-01-10T08:00:00-09:00")

    def test_already_aware_untouched(self):
        self.assertEqual(fpc.alaska_iso("2026-08-20T18:19:45-08:00"),
                         "2026-08-20T18:19:45-08:00")
        self.assertIsNone(fpc.alaska_iso(None))


class TestAdsbEvents(unittest.TestCase):
    def test_growing_hold_replaces_its_earlier_snapshot(self):
        # A hold still in progress re-scans with a longer last_seen and the
        # same key; the archive must take the newer one, not keep the stub.
        stub = {"pattern_type": "hold", "hex_id": "a03000",
                "detected_at": "2026-08-20T18:22:00-08:00",
                "last_seen": "2026-08-20T18:30:00-08:00", "duration_min": 8}
        grown = dict(stub, last_seen="2026-08-20T18:34:00-08:00", duration_min=12)
        merged = fpc.merge_detections([stub], [grown])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["duration_min"], 12)


class TestBuildOutput(unittest.TestCase):
    def test_counts_by_type(self):
        grid = fpc.normalize_detection(AER66_GRID, "grid")
        circle = fpc.normalize_detection(
            dict(AER66_GRID, hex_id="c22222", pattern_type="circle"), "circle"
        )
        now = datetime(2026, 8, 4, 14, 32, 10, tzinfo=ZoneInfo("America/Anchorage"))
        out = fpc.build_output([grid, circle], now)
        self.assertEqual(out["counts"]["total"], 2)
        self.assertEqual(out["counts"]["circles"], 1)
        self.assertEqual(out["counts"]["grids"], 1)

    def test_source_online_defaults_true(self):
        now = datetime(2026, 8, 4, 14, 32, 10, tzinfo=ZoneInfo("America/Anchorage"))
        self.assertTrue(fpc.build_output([], now)["source_online"])
        self.assertFalse(fpc.build_output([], now, source_online=False)["source_online"])

    def test_counts_go_arounds_and_holds(self):
        now = datetime(2026, 8, 4, 14, 32, 10, tzinfo=ZoneInfo("America/Anchorage"))
        dets = [{"pattern_type": "go_around"}, {"pattern_type": "hold"},
                {"pattern_type": "hold"}, {"pattern_type": "circle"}]
        c = fpc.build_output(dets, now)["counts"]
        self.assertEqual((c["go_arounds"], c["holds"], c["circles"], c["grids"]),
                         (1, 2, 1, 0))
        self.assertEqual(c["total"], 4)

    def test_display_timestamp_carries_alaska_suffix(self):
        now = datetime(2026, 8, 4, 14, 32, 10, tzinfo=ZoneInfo("America/Anchorage"))
        out = fpc.build_output([], now)
        self.assertEqual(out["generated_display"], "August 04, 2026 at 02:32 PM AKDT")
        self.assertFalse(out["generated_display"].endswith(" "))

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            fpc.build_output([], datetime(2026, 8, 4, 14, 32, 10))


class TestArchiveIO(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = f"{self.tmpdir}/flight_patterns.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_existing_returns_empty_when_absent(self):
        self.assertEqual(fpc.load_existing(self.path), [])

    def test_load_existing_raises_on_corrupt_file(self):
        # A present-but-unparseable file must NOT be treated as empty: that
        # would let local corruption silently wipe the archive. Only a
        # missing file is safe to rebuild from.
        with open(self.path, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(ValueError):
            fpc.load_existing(self.path)

    def test_corrupt_archive_survives_a_failed_run_untouched(self):
        import unittest.mock as mock
        with open(self.path, "wb") as fh:
            fh.write(b"{not json")
        with open(self.path, "rb") as fh:
            before = fh.read()

        original_output_file = fpc.OUTPUT_FILE
        original_data_dir = fpc.DATA_DIR
        fpc.OUTPUT_FILE = self.path
        fpc.DATA_DIR = os.path.dirname(self.path)
        try:
            with mock.patch.object(
                fpc, "fetch_history", return_value={"circles": [], "grids": []}
            ):
                rc = fpc.main()
        finally:
            fpc.OUTPUT_FILE = original_output_file
            fpc.DATA_DIR = original_data_dir

        with open(self.path, "rb") as fh:
            after = fh.read()

        self.assertEqual(rc, 1)
        self.assertEqual(before, after)

    def test_write_atomic_failure_leaves_no_tmp_file(self):
        import os as _os
        # A set() is not JSON-serializable, so json.dump raises mid-write.
        bad_payload = {"detections": [{"bad": {1, 2, 3}}]}
        with self.assertRaises(TypeError):
            fpc.write_atomic(self.path, bad_payload)
        self.assertFalse(_os.path.exists(self.path + ".tmp"))

    def test_write_atomic_roundtrips(self):
        now = datetime(2026, 8, 4, 14, 32, 10, tzinfo=ZoneInfo("America/Anchorage"))
        payload = fpc.build_output([fpc.normalize_detection(AER66_GRID, "grid")], now)
        fpc.write_atomic(self.path, payload)
        self.assertEqual(fpc.load_existing(self.path)[0]["callsign"], "AER66")

    def test_write_atomic_leaves_no_tmp_file(self):
        import os as _os
        now = datetime(2026, 8, 4, 14, 32, 10, tzinfo=ZoneInfo("America/Anchorage"))
        fpc.write_atomic(self.path, fpc.build_output([], now))
        self.assertFalse(_os.path.exists(self.path + ".tmp"))

    def test_extract_detections_flattens_both_arrays(self):
        payload = {"circles": [dict(AER66_GRID, hex_id="c11111", pattern_type="circle")],
                   "grids": [AER66_GRID]}
        dets = fpc.extract_detections(payload)
        self.assertEqual(len(dets), 2)
        self.assertEqual({d["pattern_type"] for d in dets}, {"circle", "racetrack"})

    def test_extract_detections_tolerates_missing_arrays(self):
        self.assertEqual(fpc.extract_detections({}), [])

    def test_extract_detections_skips_unusable_records(self):
        payload = {"circles": [{"callsign": "NOPOS", "hex_id": "x"}], "grids": []}
        self.assertEqual(fpc.extract_detections(payload), [])


if __name__ == "__main__":
    unittest.main()
