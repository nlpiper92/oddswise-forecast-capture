"""Offline tests (no network): python -m unittest test_capture -v"""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import capture

GAME = {"source": "espn", "event_id": "1", "home": "A", "away": "B", "kickoff_utc": "2026-10-31T23:30:00Z",
        "time_valid": True, "neutral_site": False, "venue_id": "9", "venue": "Stadium", "city": "Charlotte",
        "state": "NC", "country": "USA", "indoor": False}


def periods(first="2026-10-31T12:00:00Z", n=24):
    t0 = capture.parse_time(first)
    return [{"number": i + 1, "startTime": (t0 + dt.timedelta(hours=i)).isoformat(), "temperature": 50 + i}
            for i in range(n)]


class Tests(unittest.TestCase):
    def test_parse_game(self):
        ev = {"id": "7", "date": "2026-10-30T23:00Z", "competitions": [{
            "timeValid": True, "neutralSite": False,
            "venue": {"id": "4", "fullName": "Dome", "address": {"city": "X", "state": "TX", "country": "USA"},
                      "indoor": True},
            "competitors": [{"homeAway": "home", "team": {"displayName": "H"}},
                            {"homeAway": "away", "team": {"displayName": "A"}}]}]}
        g = capture.parse_game(ev)
        self.assertEqual((g["home"], g["away"], g["indoor"], g["kickoff_utc"]), ("H", "A", True, "2026-10-30T23:00:00Z"))

    def test_window_is_kickoff_hour_plus_minus_3(self):
        window, has_hour = capture.kickoff_window(periods(), GAME["kickoff_utc"])
        self.assertEqual(len(window), 7)
        self.assertTrue(has_hour)

    def test_window_flags_missing_kickoff_hour(self):
        _, has_hour = capture.kickoff_window(periods("2026-10-31T12:00:00Z", 5), GAME["kickoff_utc"])
        self.assertFalse(has_hour)

    def test_location_ambiguous_is_low_confidence(self):
        res = [{"country_code": "US", "admin1": "Ohio", "latitude": 1, "longitude": 1, "population": 1000, "name": "X"},
               {"country_code": "US", "admin1": "Ohio", "latitude": 2, "longitude": 2, "population": 900, "name": "X"}]
        self.assertEqual(capture.pick_location(res, "OH")[2], "low")

    def test_location_clear_winner_is_high(self):
        res = [{"country_code": "US", "admin1": "Ohio", "latitude": 1, "longitude": 1, "population": 100000, "name": "X"},
               {"country_code": "US", "admin1": "Ohio", "latitude": 2, "longitude": 2, "population": 900, "name": "X"},
               {"country_code": "US", "admin1": "Texas", "latitude": 3, "longitude": 3, "population": 5, "name": "X"}]
        self.assertEqual(capture.pick_location(res, "OH")[:3], (1, 1, "high"))

    def test_indoor_not_fetched_makes_no_requests(self):
        with mock.patch.object(capture, "fetch_json", side_effect=AssertionError("no network")):
            rec = capture.capture_game({**GAME, "indoor": True}, {}, {})
        self.assertEqual(rec["status"], "indoor_not_fetched")

    def test_captured_record_keeps_raw_data_and_issue_time(self):
        loc = {"9": {"lat": 1, "lon": 2, "confidence": "high", "forecast_hourly": "http://nws/hourly"}}
        resp = {"properties": {"updateTime": "U", "generatedAt": "G", "periods": periods()}}
        with mock.patch.object(capture, "fetch_json", return_value=(resp, "LM")):
            rec = capture.capture_game(GAME, loc, {})
        self.assertEqual(rec["status"], "captured")
        self.assertEqual(rec["issued"]["updateTime"], "U")
        self.assertEqual(rec["raw_periods"], capture.kickoff_window(resp["properties"]["periods"], GAME["kickoff_utc"])[0])
        self.assertEqual(rec["source_url"], "http://nws/hourly")

    def test_failure_is_recorded_not_raised(self):
        loc = {"9": {"lat": 1, "lon": 2, "confidence": "high", "forecast_hourly": "u"}}
        with mock.patch.object(capture, "fetch_json", side_effect=OSError("boom")):
            rec = capture.capture_game(GAME, loc, {})
        self.assertEqual(rec["status"], "failed")

    def test_override_is_used_and_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "o.json"
            f.write_text(json.dumps([{"venue": "stadium", "city": "CHARLOTTE", "state": "nc", "lat": 5.5, "lon": 6.6}]))
            with mock.patch.object(capture, "OVERRIDES", f), mock.patch.object(capture, "fetch_json", side_effect=AssertionError("no geocoder")):
                loc = capture.locate(GAME, {})
        self.assertEqual((loc["lat"], loc["lon"], loc["override"]), (5.5, 6.6, True))

    def test_append_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            capture.append_lines(p, ["a"])
            capture.append_lines(p, ["b"])
            self.assertEqual(p.read_text().split(), ["a", "b"])

    def test_write_new_gz_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "captures" / "run-1.jsonl.gz"
            capture.write_new(p, ['{"a": 1}'])
            with self.assertRaises(FileExistsError):
                capture.write_new(p, ["x"])
            self.assertEqual(list(capture.read_records(Path(d))), [{"a": 1}])

    def test_report_merges_pc_and_cloud_without_double_counting(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            pc, cloud = Path(d) / "pc", Path(d) / "cloud"
            run_line = "start=2026-10-27T00:00:00Z end=x expected=1 captured=1 indoor=0 failed=0 status=ok"
            rec = {"game": GAME, "status": "captured", "fetched_at_utc": "2026-10-27T01:00:00Z"}
            (pc / "captures").mkdir(parents=True)
            (pc / "captures" / "2026-10-27.jsonl").write_text(json.dumps(rec) + "\n")
            (pc / "runs.log").write_text(run_line + "\n")
            capture.write_new(cloud / "captures" / "run-a.jsonl.gz", [json.dumps({**rec, "fetched_at_utc": "2026-10-27T02:00:00Z"})])
            capture.write_new(cloud / "runs" / "run-a.log", [run_line.strip()])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                capture.report(dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc), roots=[pc, cloud])
        out = buf.getvalue()
        self.assertIn("games seen 1 | captured 1", out)
        self.assertIn("runs [pc]", out)
        self.assertIn("runs [cloud]", out)


if __name__ == "__main__":
    unittest.main()
