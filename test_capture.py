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


class VenueOverrideRegressionTests(unittest.TestCase):
    """Real affected venue identities; network-free, all writes stay in a temporary directory."""

    CASES = (
        ("3683", "Kroger Field", "Lexington", "KY", 38.02274, -84.50515,
         "2026-11-14T23:15:00Z"),
        ("3696", "Zoeller Field at Dix Stadium", "Kent", "OH", 41.13905, -81.31367,
         "2026-11-18T00:00:00Z"),
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Use the checked-in data, while isolating every mutable cache and log.
        self.overrides = Path(__file__).with_name("venue_overrides.json")
        self.cache_path = Path(self.tmp.name) / "venue_cache.json"
        self.patches = (
            mock.patch.object(capture, "OVERRIDES", self.overrides),
            mock.patch.object(capture, "VENUE_CACHE", self.cache_path),
        )
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def game(self, case):
        venue_id, venue, city, state, _, _, kickoff = case
        return {**GAME, "venue_id": venue_id, "venue": venue, "city": city,
                "state": state, "kickoff_utc": kickoff}

    def negative_cache(self, case):
        return {case[0]: {"venue": case[1], "city": case[2], "state": case[3],
                         "lat": None, "lon": None, "confidence": "low",
                         "note": "2 similar matches, ambiguous"}}

    def test_required_stadium_coordinates_and_sources_are_present(self):
        entries = json.loads(self.overrides.read_text(encoding="utf-8"))
        keys = [tuple(e[k].strip().lower() for k in ("venue", "city", "state"))
                for e in entries]
        self.assertEqual(len(keys), len(set(keys)), "duplicate normalized override")
        for case in self.CASES:
            with self.subTest(venue=case[1]):
                entry = capture.find_override(self.game(case))
                self.assertIsNotNone(entry)
                self.assertEqual((entry["lat"], entry["lon"]), (case[4], case[5]))
                self.assertIn("https://", entry["source"])
        # The pre-existing Norfolk pin must remain available and unchanged.
        norfolk = capture.find_override(
            {**GAME, "venue": "S.B. Ballard Stadium", "city": "Norfolk", "state": "VA"})
        self.assertEqual((norfolk["lat"], norfolk["lon"]), (36.8889, -76.3049))

    def test_pins_override_negative_cache_without_geocoding_or_erasing_history(self):
        for case in self.CASES:
            with self.subTest(venue=case[1]):
                cache = self.negative_cache(case)
                old = dict(cache[case[0]])
                with mock.patch.object(capture, "fetch_json",
                                       side_effect=AssertionError("no geocoder")):
                    loc = capture.locate(self.game(case), cache)
                self.assertEqual((loc["lat"], loc["lon"]), (case[4], case[5]))
                self.assertEqual(loc["confidence"], "high")
                self.assertTrue(loc["override"])
                self.assertEqual(cache[case[0]], old)
                self.assertFalse(self.cache_path.exists())

    def test_matching_is_case_and_whitespace_insensitive(self):
        for case in self.CASES:
            with self.subTest(venue=case[1]):
                game = self.game(case)
                game.update({k: "  " + game[k].swapcase() + "  "
                             for k in ("venue", "city", "state")})
                pin = capture.find_override(game)
                self.assertIsNotNone(pin)
                self.assertEqual((pin["lat"], pin["lon"]), (case[4], case[5]))

    def test_unmatched_city_state_or_name_is_not_guessed(self):
        for case in self.CASES:
            for field in ("venue", "city", "state"):
                with self.subTest(venue=case[1], field=field):
                    game = self.game(case)
                    game[field] = "not the same " + game[field]
                    self.assertIsNone(capture.find_override(game))
                    with mock.patch.object(capture, "fetch_json",
                                           side_effect=AssertionError("no request")):
                        rec = capture.capture_game(game, self.negative_cache(case), {})
                    self.assertEqual(rec["status"], "no_location")

    def test_capture_uses_pin_and_preserves_raw_forecast_and_game_identity(self):
        for case in self.CASES:
            with self.subTest(venue=case[1]):
                game = self.game(case)
                hour = capture.parse_time(game["kickoff_utc"]).replace(minute=0)
                raw = periods((hour - dt.timedelta(hours=3)).isoformat(), 7)
                hourly = "https://api.weather.gov/gridpoints/TEST/1,2/forecast/hourly"
                points_url = capture.NWS_POINTS.format(lat=case[4], lon=case[5])
                responses = {
                    points_url: ({"properties": {"forecastHourly": hourly}}, None),
                    hourly: ({"properties": {"periods": raw, "updateTime": "ISSUED",
                                             "generatedAt": "GENERATED"}}, "LAST-MODIFIED"),
                }
                cache = self.negative_cache(case)
                with mock.patch.object(capture, "fetch_json",
                                       side_effect=lambda url: responses[url]) as fetch:
                    rec = capture.capture_game(game, cache, {})
                self.assertEqual(rec["status"], "captured")
                self.assertEqual(rec["game"], game)
                self.assertTrue(rec["location"]["override"])
                self.assertEqual(rec["raw_periods"], raw)
                self.assertEqual(rec["source_url"], hourly)
                self.assertEqual(rec["issued"]["updateTime"], "ISSUED")
                self.assertEqual(fetch.call_args_list,
                                 [mock.call(points_url), mock.call(hourly)])
                self.assertEqual(cache[case[0]]["confidence"], "low")
                persisted = json.loads(self.cache_path.read_text(encoding="utf-8"))
                self.assertEqual(persisted[case[0]], self.negative_cache(case)[case[0]])
                pin_key = f"override|{case[1]}|{case[2]}|{case[3]}"
                self.assertEqual(persisted[pin_key]["forecast_hourly"], hourly)
                # A later process can reuse the pin and grid URL without geocoding
                # or a second points request; the original failure stays intact.
                with mock.patch.object(capture, "fetch_json",
                                       side_effect=lambda url: responses[url]) as fetch:
                    again = capture.capture_game(game, persisted, {})
                self.assertEqual(again["status"], "captured")
                self.assertEqual(fetch.call_args_list, [mock.call(hourly)])
                self.assertEqual(persisted[case[0]], self.negative_cache(case)[case[0]])

    def test_resolved_venue_does_not_invent_an_unavailable_forecast(self):
        for case in self.CASES:
            with self.subTest(venue=case[1]):
                hourly = "https://api.weather.gov/gridpoints/TEST/1,2/forecast/hourly"
                with mock.patch.object(capture, "fetch_json", side_effect=[
                    ({"properties": {"forecastHourly": hourly}}, None),
                    ({"properties": {"periods": [], "updateTime": "ISSUED"}}, None),
                ]):
                    rec = capture.capture_game(self.game(case), self.negative_cache(case), {})
                self.assertEqual(rec["status"], "too_far")
                self.assertTrue(rec["location"]["override"])
                self.assertNotIn("raw_periods", rec)

    def test_indoor_game_with_a_pin_still_is_not_fetched(self):
        for case in self.CASES:
            with self.subTest(venue=case[1]):
                with mock.patch.object(capture, "fetch_json",
                                       side_effect=AssertionError("no request")):
                    rec = capture.capture_game({**self.game(case), "indoor": True},
                                               self.negative_cache(case), {})
                self.assertEqual(rec["status"], "indoor_not_fetched")


if __name__ == "__main__":
    unittest.main()
