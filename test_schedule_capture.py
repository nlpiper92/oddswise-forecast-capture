"""Offline scheduler tests: python -m unittest discover -v"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import schedule_capture as scheduler

NOW = dt.datetime(2026, 11, 16, 13, 37, tzinfo=dt.timezone.utc)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def receipt(self, hour, status="ok", expected=1, archive=True):
        tag = f"20261116T{hour:02d}0000Z"
        (self.root / "runs").mkdir(exist_ok=True)
        (self.root / "captures").mkdir(exist_ok=True)
        (self.root / "runs" / f"run-{tag}.log").write_text(
            f"start=2026-11-16T{hour:02d}:00:00Z expected={expected} status={status}\n")
        if archive:
            (self.root / "captures" / f"run-{tag}.jsonl.gz").write_bytes(b"fixture")

    def test_empty_history_dispatches(self):
        self.assertEqual(scheduler.decision(self.root, NOW, []), "dispatch")

    def test_prior_slot_does_not_suppress(self):
        self.receipt(6)
        self.assertEqual(scheduler.decision(self.root, NOW, []), "dispatch")

    def test_same_slot_suppresses_duplicates(self):
        self.receipt(12)
        self.assertNotEqual(scheduler.decision(self.root, NOW, []), "dispatch")

    def test_crashed_pass_is_retried(self):
        self.receipt(12, status="crashed")
        self.assertEqual(scheduler.decision(self.root, NOW, []), "dispatch")

    def test_missing_archive_is_not_capture_evidence(self):
        self.receipt(12, archive=False)
        self.assertEqual(scheduler.decision(self.root, NOW, []), "dispatch")

    def test_zero_games_pass_needs_no_archive(self):
        self.receipt(12, expected=0, archive=False)
        self.assertNotEqual(scheduler.decision(self.root, NOW, []), "dispatch")

    def test_all_active_statuses_suppress_dispatch(self):
        for status in ("queued", "in_progress", "requested", "waiting", "pending"):
            with self.subTest(status=status):
                self.assertEqual(scheduler.decision(self.root, NOW, [{"status": status}]),
                                 "capture already active")

    def test_failed_cancelled_completed_runs_do_not_fill_slot(self):
        for conclusion in ("failure", "cancelled", "timed_out", "skipped"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(scheduler.decision(self.root, NOW,
                    [{"status": "completed", "conclusion": conclusion}]), "dispatch")

    def test_recent_success_prevents_archive_visibility_race(self):
        run = {"status": "completed", "conclusion": "success", "created_at": "2026-11-16T12:37:00Z"}
        self.assertNotEqual(scheduler.decision(self.root, NOW, [run]), "dispatch")
        run["created_at"] = "2026-11-16T06:37:00Z"
        self.assertEqual(scheduler.decision(self.root, NOW, [run]), "dispatch")

    def test_future_or_unzoned_timestamp_refused(self):
        self.receipt(18)
        with self.assertRaises(ValueError):
            scheduler.decision(self.root, NOW, [])
        with self.assertRaises(ValueError):
            scheduler.timestamp("2026-11-16T06:00:00")

    def test_slot_boundaries_and_timezone(self):
        for hour in range(24):
            at = NOW.replace(hour=hour)
            self.assertEqual(scheduler.slot_start(at).hour, hour // 6 * 6)
        self.assertEqual(scheduler.slot_start(NOW.astimezone(dt.timezone(dt.timedelta(hours=-5)))),
                         NOW.replace(hour=12, minute=0))

    def test_missed_primary_wake_recovers_next_hour_without_backfill(self):
        # No 12:37 event: 13:37 dispatches current slot, then later wakes skip.
        self.receipt(6)
        self.assertEqual(scheduler.decision(self.root, NOW, []), "dispatch")
        self.receipt(13)
        self.assertNotEqual(scheduler.decision(self.root, NOW.replace(hour=14), []), "dispatch")
        self.assertEqual(len(list((self.root / "runs").glob("*.log"))), 2)

    def test_successful_four_slot_day(self):
        for hour in (0, 6, 12, 18):
            at = NOW.replace(hour=hour)
            self.assertEqual(scheduler.decision(self.root, at, []), "dispatch")
            self.receipt(hour)
            for retry in range(hour, hour + 6):
                self.assertNotEqual(scheduler.decision(self.root, at.replace(hour=retry), []), "dispatch")

    def test_no_network_on_wrong_repository_or_branch(self):
        with mock.patch.dict(scheduler.os.environ, {"GITHUB_REPOSITORY": scheduler.REPO,
                                                   "GITHUB_REF": "refs/heads/other"}), \
             mock.patch.object(scheduler, "api") as api:
            with self.assertRaises(ValueError):
                scheduler.main()
            api.assert_not_called()

    def test_api_post_request_contract(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b""
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(scheduler.urllib.request, "build_opener", return_value=opener):
            scheduler.api("POST", "dispatches", "test-only", {"ref": "main"})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url,
            "https://api.github.com/repos/nlpiper92/oddswise-forecast-capture/actions/workflows/capture.yml/dispatches")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(scheduler.json.loads(request.data), {"ref": "main"})

    def test_dispatch_accepted_not_retried(self):
        with mock.patch.dict(scheduler.os.environ, {"GITHUB_REPOSITORY": scheduler.REPO,
              "GITHUB_REF": "refs/heads/main", "GH_TOKEN": "test-only"}), \
             mock.patch.object(scheduler, "ROOT", self.root), \
             mock.patch.object(scheduler, "api", side_effect=[{"workflow_runs": []}, TimeoutError]) as api:
            with self.assertRaises(TimeoutError):
                scheduler.main()
            self.assertEqual(api.call_count, 2)
            self.assertEqual(api.call_args.args[:2], ("POST", "dispatches"))


if __name__ == "__main__":
    unittest.main()
