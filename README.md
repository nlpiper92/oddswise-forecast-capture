# OddsWise forecast capture (cloud copy)

Saves National Weather Service hourly forecasts, exactly as issued, for upcoming FBS college football
games. Forecasts can't be reconstructed later, so this runs on a schedule even when the PC is off.
Capture only: no models, no adjustments, no backfill.

**When it runs:** the separate `capture-scheduler` workflow checks hourly at :37. It dispatches
`capture` only if the current six-hour UTC slot (00–06, 06–12, 12–18, 18–24) lacks a completed pass
and no capture is queued/running. This gives a missed scheduled event another chance during the
same slot, targeting four captures/day without fetching forecasts on every hourly check. Manual
passes remain available via Actions > capture > Run workflow and count toward the slot.

GitHub schedules are best-effort, not an exact-time guarantee. If all hourly events in a slot
are missed, the slot still cannot be captured. Avoid manually dispatching a capture at the same
time as a scheduler check: API visibility delay can allow an extra serialized pass. A fully missed slot is not backfilled:
forecasts are archived at their real fetch/issue times. The old PC capture task stays retired.
No new credential is needed: the scheduler uses the built-in repository token with Actions write
permission solely to dispatch the existing capture workflow. Capture keeps its existing repository
contents-write permission. Concurrent captures remain serialized.

**Monitoring:** the Assembly Line Viewer continues watching `capture.yml`, never the hourly
scheduler checks, so a successful no-op check is not reported as a new capture. Its existing
18-hour staleness rule is unchanged and is not a three-to-four-daily cadence guarantee.
Use the actual `runs/*.log` records to validate daily frequency, and inspect `capture-scheduler`
if captures stop. Disabling capture alone is not the complete off switch; disable the scheduler too.

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

**Switch off:** disable `capture-scheduler` first, then `capture`, through Actions > workflow > "..." > Disable workflow.

`capture.py` here is a copy of the one in the PC folder; change it in one place and copy it across.

## Cadence repair validation

Run `python -m unittest discover -v` for network-free capture and scheduler tests. Scheduler tests
cover missed wakes, four-slot days, duplicate suppression, active/queued captures, failed passes,
empty game slates, archive visibility and ambiguous dispatch outcomes. No API POST is retried
blindly; a later hourly check reads actual workflow history first.

After activation, verify one real scheduler-to-capture dispatch and an archive commit, then observe
at least two full UTC days for the target daily frequency. Tests do not establish future event
delivery. Missing cron-event causes remain unconfirmed; GitHub documents delayed/dropped schedule
events at https://docs.github.com/en/actions/how-tos/troubleshoot-workflows .
