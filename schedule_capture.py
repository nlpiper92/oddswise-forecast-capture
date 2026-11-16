"""Hourly GitHub wake: aim for one completed capture per six-hour UTC slot.

No forecast requests, archive writes or backfill. Run only from the serialized
scheduler workflow. A manual capture still runs directly via capture.yml.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import urllib.request

REPO = "nlpiper92/oddswise-forecast-capture"
BRANCH = "main"
WORKFLOW = "capture.yml"
ROOT = Path(__file__).resolve().parent


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Run timestamp has no timezone")
    return result.astimezone(dt.timezone.utc)


def slot_start(now):
    now = now.astimezone(dt.timezone.utc)
    return now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)


def slot_captured(root, now):
    """A persisted completed pass, not a green scheduler wake, fills the slot."""
    start = slot_start(now)
    for path in sorted((root / "runs").glob("run-*.log"), reverse=True):
        for line in path.read_text(encoding="utf-8").splitlines():
            row = dict(re.findall(r"(\w+)=(\S+)", line))
            at = timestamp(row["start"])
            if at > now:
                raise ValueError("Future capture timestamp")
            if at < start or row.get("status") != "ok":
                continue
            expected = int(row["expected"])
            if expected < 0:
                raise ValueError("Negative game count")
            archive = root / "captures" / (path.stem + ".jsonl.gz")
            if expected == 0 or archive.is_file():
                return True
    return False


def decision(root, now, runs):
    # Do not enqueue behind a running/queued/manual capture, even from an older slot.
    if any(run.get("status") != "completed" for run in runs):
        return "capture already active"
    if slot_captured(root, now):
        return "current UTC slot already captured"
    # Workflow dispatch may precede the archive commit becoming visible. A recent
    # successful capture also suppresses a duplicate until the next hourly wake.
    if any(run.get("conclusion") == "success" and
           timestamp(run["created_at"]) >= slot_start(now) for run in runs):
        return "successful capture in this slot; await archive visibility"
    return "dispatch"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, target):
        raise ValueError("GitHub API redirect refused")


def api(method, suffix, token, data=None):
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/{suffix}"
    request = urllib.request.Request(url, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "OddsWise-Capture-Scheduler"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None


def main():
    if os.environ.get("GITHUB_REPOSITORY") != REPO or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Scheduler must run on the capture repository's main branch")
    token = os.environ["GH_TOKEN"]
    history = api("GET", "runs?branch=main&per_page=100", token)
    runs = history["workflow_runs"]
    if not isinstance(runs, list):
        raise ValueError("Invalid capture history")
    # Fail safely if 100 recent attempts crowd out this slot, rather than
    # assuming an omitted successful/active attempt does not exist.
    if len(runs) == 100 and timestamp(runs[-1]["created_at"]) >= slot_start(dt.datetime.now(dt.timezone.utc)):
        raise ValueError("Capture slot history needs pagination")
    now = dt.datetime.now(dt.timezone.utc)
    action = decision(ROOT, now, runs)
    print(f"slot={slot_start(now).isoformat()} decision={action}")
    if action == "dispatch":
        # Do not retry an uncertain POST: next hourly wake checks actual history.
        api("POST", "dispatches", token, {"ref": BRANCH})
        print("Capture dispatch accepted; this is not proof of completed capture.")


if __name__ == "__main__":
    main()
