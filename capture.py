"""OddsWise forecast capture: saves NWS hourly forecasts exactly as issued, for upcoming
college football games. Capture only: no interpretation, no backfill. Append-only storage.

  python capture.py run      one capture pass
  python capture.py report [folder ...]   weekly summary (give the PC and cloud folders to merge them)
"""
import datetime as dt
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAPTURES = ROOT / "captures"
RUNS_LOG = ROOT / "runs.log"
VENUE_CACHE = ROOT / "venue_cache.json"
OVERRIDES = ROOT / "venue_overrides.json"
UA = {"User-Agent": "OddsWiseForecastCapture/1.0", "Accept": "application/geo+json, application/json"}
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
GEOCODER = "https://geocoding-api.open-meteo.com/v1/search"
NWS_POINTS = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
DAYS_AHEAD = 8
PAUSE = 0.5
RUN_EVERY_HOURS = 6

STATES = dict(zip(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
    "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split(),
    ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
     "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
     "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
     "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
     "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
     "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
     "West Virginia", "Wisconsin", "Wyoming"]))


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def fetch_json(url, retries=4):
    """Plain read-only GET with a polite pause. Retries temporary server errors (NWS has them often)."""
    for attempt in range(retries):
        time.sleep(PAUSE if attempt == 0 else 3 * attempt)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r), r.headers.get("Last-Modified")
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise


# ---------- schedule ----------
def parse_game(ev):
    comp = ev["competitions"][0]
    venue = comp.get("venue") or {}
    addr = venue.get("address") or {}
    teams = {c["homeAway"]: c["team"]["displayName"] for c in comp["competitors"]}
    return {
        "source": "espn", "event_id": ev["id"],
        "home": teams.get("home"), "away": teams.get("away"),
        "kickoff_utc": iso(parse_time(ev["date"])), "time_valid": comp.get("timeValid"),
        "neutral_site": comp.get("neutralSite"),
        "venue_id": venue.get("id"), "venue": venue.get("fullName"),
        "city": addr.get("city"), "state": addr.get("state"), "country": addr.get("country"),
        "indoor": venue.get("indoor"),
    }


def load_schedule(now):
    """ESPN's date-range query is rejected, so ask one day at a time (FBS = groups 80)."""
    games = {}
    for i in range(DAYS_AHEAD + 1):
        day = (now + dt.timedelta(days=i)).strftime("%Y%m%d")
        data, _ = fetch_json(f"{ESPN}?dates={day}&groups=80&limit=300")
        for ev in data.get("events", []):
            g = parse_game(ev)
            if now <= parse_time(g["kickoff_utc"]) <= now + dt.timedelta(days=DAYS_AHEAD):
                games[g["event_id"]] = g
    return sorted(games.values(), key=lambda g: g["kickoff_utc"])


# ---------- venue locations ----------
def pick_location(results, state_abbr):
    """Return (lat, lon, confidence, note). Low confidence is flagged, never guessed."""
    state = STATES.get(state_abbr)
    hits = [r for r in results if r.get("country_code") == "US" and r.get("admin1") == state]
    if not hits:
        return None, None, "low", "no geocoder match in that state"
    hits.sort(key=lambda r: r.get("population") or 0, reverse=True)
    if len(hits) > 1 and (hits[0].get("population") or 0) < 5 * (hits[1].get("population") or 0) + 1:
        return None, None, "low", f"{len(hits)} similar matches, ambiguous"
    return hits[0]["latitude"], hits[0]["longitude"], "high", f"matched {hits[0]['name']}, {state}"


def find_override(game):
    """Pinned lat/lon from venue_overrides.json, matched on venue name + city + state (case-insensitive)."""
    if not OVERRIDES.exists():
        return None
    want = [str(game[k] or "").strip().lower() for k in ("venue", "city", "state")]
    for o in json.loads(OVERRIDES.read_text(encoding="utf-8")):
        if [o[k].strip().lower() for k in ("venue", "city", "state")] == want:
            return o
    return None


def locate(game, cache):
    o = find_override(game)
    if o:
        key = f"override|{o['venue']}|{o['city']}|{o['state']}"
        if (cache.get(key) or {}).get("lat") != o["lat"] or cache[key].get("lon") != o["lon"]:
            cache[key] = {"venue": o["venue"], "city": o["city"], "state": o["state"], "lat": o["lat"],
                          "lon": o["lon"], "confidence": "high", "override": True,
                          "note": "location pinned by venue_overrides.json (not geocoded)"}
        return cache[key]
    key = game["venue_id"] or f"{game['venue']}|{game['city']}|{game['state']}"
    if key in cache:
        return cache[key]
    if not game["city"] or not game["state"]:
        return {"confidence": "low", "note": "venue has no city/state in schedule"}
    if game["country"] not in (None, "USA"):
        return {"confidence": "low", "note": f"venue outside the US ({game['country']})"}
    q = urllib.parse.urlencode({"name": game["city"], "count": 20, "country_code": "US", "format": "json"})
    data, _ = fetch_json(f"{GEOCODER}?{q}")
    lat, lon, conf, note = pick_location(data.get("results", []), game["state"])
    loc = {"venue": game["venue"], "city": game["city"], "state": game["state"],
           "lat": lat, "lon": lon, "confidence": conf, "note": note}
    cache[key] = loc
    VENUE_CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return loc


def hourly_url(loc, cache):
    """NWS grid lookup for a location, cached with the venue."""
    if not loc.get("forecast_hourly"):
        data, _ = fetch_json(NWS_POINTS.format(lat=loc["lat"], lon=loc["lon"]))
        loc["forecast_hourly"] = data["properties"]["forecastHourly"]
        VENUE_CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return loc["forecast_hourly"]


# ---------- capture ----------
def kickoff_window(periods, kickoff):
    """Raw hourly periods from kickoff hour minus 3h to plus 3h, exactly as received."""
    hour = parse_time(kickoff).replace(minute=0, second=0, microsecond=0)
    lo, hi = hour - dt.timedelta(hours=3), hour + dt.timedelta(hours=3)
    window = [p for p in periods if lo <= parse_time(p["startTime"]) <= hi]
    has_kickoff_hour = any(parse_time(p["startTime"]) == hour for p in window)
    return window, has_kickoff_hour


def capture_game(game, loc_cache, forecasts):
    """Return the record for one game. status: captured / indoor_not_fetched / no_location / too_far / failed."""
    rec = {"fetched_at_utc": iso(utcnow()), "game": game}
    if game["indoor"] is True:
        return {**rec, "status": "indoor_not_fetched", "reason": "indoor, not fetched"}
    try:
        loc = locate(game, loc_cache)
        if loc.get("confidence") != "high":
            return {**rec, "status": "no_location", "reason": loc.get("note"), "location": loc}
        url = hourly_url(loc, loc_cache)
        if url not in forecasts:
            forecasts[url] = (*fetch_json(url), iso(utcnow()))
        data, last_modified, fetched = forecasts[url]
        rec["fetched_at_utc"] = fetched
        props = data["properties"]
        window, has_hour = kickoff_window(props["periods"], game["kickoff_utc"])
        rec.update(source_url=url, location=loc,
                   issued={"updateTime": props.get("updateTime"), "generatedAt": props.get("generatedAt"),
                           "http_last_modified": last_modified})
        if not has_hour:
            return {**rec, "status": "too_far", "reason": "kickoff hour not in the forecast yet"}
        meta = {k: v for k, v in props.items() if k != "periods"}
        return {**rec, "status": "captured", "response_meta": meta, "raw_periods": window}
    except Exception as e:  # one bad game must not stop the pass
        return {**rec, "status": "failed", "reason": f"{type(e).__name__}: {e}"}


def append_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def write_new(path, lines):
    """Cloud mode: every pass gets its own new file, so nothing is ever edited or conflicts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "xt", encoding="utf-8") as f:  # "x" = refuse to overwrite
        for line in lines:
            f.write(line + "\n")


def run():
    start = utcnow()
    cloud = os.environ.get("CAPTURE_MODE") == "cloud"
    counts = dict.fromkeys(["expected", "captured", "indoor_not_fetched", "no_location", "too_far", "failed"], 0)
    status = "ok"
    try:
        games = load_schedule(start)
        counts["expected"] = len(games)
        cache = json.loads(VENUE_CACHE.read_text(encoding="utf-8")) if VENUE_CACHE.exists() else {}
        forecasts, records = {}, []
        for g in games:
            rec = capture_game(g, cache, forecasts)
            counts[rec["status"]] += 1
            records.append(json.dumps(rec, ensure_ascii=False))
            if rec["status"] == "failed":
                print(f"  failed: {g['away']} at {g['home']}: {rec['reason']}")
        if records and cloud:
            write_new(CAPTURES / f"run-{start:%Y%m%dT%H%M%SZ}.jsonl.gz", records)
        elif records:
            append_lines(CAPTURES / f"{start:%Y-%m-%d}.jsonl", records)
    except Exception as e:
        status = f"crashed ({type(e).__name__}: {e})"
        print("Run stopped early:", status)
    c = counts
    line = (f"start={iso(start)} end={iso(utcnow())} expected={c['expected']} captured={c['captured']} "
            f"indoor={c['indoor_not_fetched']} no_location={c['no_location']} too_far={c['too_far']} "
            f"failed={c['failed']} status={status.split(' (')[0]}")
    if cloud:
        write_new(ROOT / "runs" / f"run-{start:%Y%m%dT%H%M%SZ}.log", [line])
    else:
        append_lines(RUNS_LOG, [line])
    print(f"Games found: {c['expected']} | captured: {c['captured']} | indoor: {c['indoor_not_fetched']} | "
          f"no usable location: {c['no_location']} | too far out yet: {c['too_far']} | failed: {c['failed']}")
    return status == "ok"


# ---------- report ----------
def week_start(t):
    d = t.date()
    return d - dt.timedelta(days=d.weekday())


def read_runs(root):
    runs = []
    for f in [root / "runs.log", *sorted((root / "runs").glob("*.log"))]:
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                kv = dict(re.findall(r"(\w+)=(\S+)", line))
                if "start" in kv:
                    runs.append(kv)
    return sorted(runs, key=lambda r: r["start"])


def read_records(root):
    for f in sorted((root / "captures").glob("*.jsonl*")):
        with (gzip.open if f.suffix == ".gz" else open)(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                yield json.loads(line)


def report(now=None, roots=None):
    """Weekly summary. Several folders (e.g. PC copy + cloud copy) are merged by game: a game counts once,
    and is 'captured' if either copy captured it. Runs are never merged: each copy is judged on its own."""
    now = now or utcnow()
    roots = [Path(r) for r in roots] if roots else [ROOT]
    runs = {r: read_runs(r) for r in roots}
    if not any(runs.values()):
        print("No runs yet.")
        return
    games = {}
    for root in roots:
        for r in read_records(root):
            g = games.setdefault(r["game"]["event_id"], {"kickoff": r["game"]["kickoff_utc"], "ok": False, "at": ""})
            g["ok"] = g["ok"] or r["status"] == "captured"
            if r["fetched_at_utc"] >= g["at"]:  # latest record decides indoor / reason
                g.update(at=r["fetched_at_utc"], status=r["status"], reason=r.get("reason"),
                         indoor=r["status"] == "indoor_not_fetched")
    weeks = {week_start(parse_time(r["start"])) for rs in runs.values() for r in rs} | {week_start(now)}
    for wk in sorted(weeks):
        wk_end = wk + dt.timedelta(days=7)
        wg = [g for g in games.values() if wk <= parse_time(g["kickoff"]).date() < wk_end]
        bad = [g for g in wg if not g["ok"] and not g["indoor"]]
        print(f"Week of {wk}: games seen {len(wg)} | captured {sum(g['ok'] for g in wg)} | "
              f"indoor {sum(g['indoor'] for g in wg)} | not captured {len(bad)}")
        for g in bad:
            print(f"    not captured ({g['status']}): {g['reason']}")
        for root, rs in runs.items():
            done = [r for r in rs if wk <= parse_time(r["start"]).date() < wk_end]
            if not rs:
                continue
            began = max(parse_time(rs[0]["start"]), dt.datetime.combine(wk, dt.time(), dt.timezone.utc))
            ended = min(now, dt.datetime.combine(wk_end, dt.time(), dt.timezone.utc))
            due = max(int((ended - began).total_seconds() // (RUN_EVERY_HOURS * 3600)), len(done))
            print(f"  runs [{root.name}]: completed {len(done)} of about {due} due | missed about "
                  f"{max(due - len(done), 0)} | with errors {sum(r.get('status') != 'ok' for r in done)} | "
                  f"failed games {sum(int(r.get('failed', 0)) for r in done)}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "run":
        sys.exit(0 if run() else 1)
    elif len(sys.argv) >= 2 and sys.argv[1] == "report":
        report(roots=sys.argv[2:])
    else:
        print(__doc__)
