# OddsWise forecast capture (cloud copy)

Saves National Weather Service hourly forecasts, exactly as issued, for upcoming FBS college football
games. Forecasts can't be reconstructed later, so this runs on a schedule even when the PC is off.
Capture only: no models, no adjustments, no backfill.

**When it runs:** every 6 hours at :17 past 00, 06, 12 and 18 UTC (Actions tab > capture > Run workflow
to start one by hand). Two passes can never overlap. It uses no secrets.

**What it saves** (each pass adds its own new files; old files are never edited):
- `captures/run-<time>.jsonl.gz` - one line per game: game identity, kickoff (UTC), venue, location and
  whether a pinned override was used, the NWS source URL, the NWS issued/updated time, our fetched-at
  time, and the raw hourly forecast for the kickoff hour plus 3 hours either side. Indoor games and games
  with no usable location get a short "why" record so counts add up.
- `runs/run-<time>.log` - one line per pass: games found, captured, indoor, no location, too far out, failed.
- `venue_cache.json` - remembered venue coordinates (a lookup cache, not capture data).
- `venue_overrides.json` - venues whose exact latitude/longitude is pinned by hand.

**Report:** `python capture.py report` (this folder), or merge the PC copy and the cloud copy:
`python capture.py report <cloud-folder> <pc-folder>`. A game counts once even if both copies saw it.
Runs are judged per copy, never merged.

**Switch off:** Actions tab > capture > "..." menu > Disable workflow.

`capture.py` here is a copy of the one in the PC folder; change it in one place and copy it across.
