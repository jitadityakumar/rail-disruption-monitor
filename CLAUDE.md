# CLAUDE.md — Rail Disruption Monitor

## Development workflow

Follow this sequence for **every** code change, no exceptions:

1. **Branch** — fetch the latest main, then create a feature branch from it:
   ```bash
   git fetch origin && git checkout -b feature/<name> origin/main
   # or: git fetch origin && git checkout -b fix/<name> origin/main
   ```
2. **Implement** — make the code changes
3. **Self-review** — run the `review` skill on the diff before raising a PR
   - Fix anything the review flags as a real issue (not just observations)
4. **PR** — push the branch and open a pull request with a clear summary and test plan
5. **Wait for local sign-off** — explicitly ask the user to check the changes locally before merging. Do not merge until they confirm.

## Pre-commit hook

Install once per checkout to block accidental secret commits:

```bash
chmod +x scripts/check-secrets.sh
ln -sf ../../scripts/check-secrets.sh .git/hooks/pre-commit
```

## Context file

`context.md` (repo root) is the canonical project context — current status, architecture decisions, next steps. It is gitignored (local only).

- **On session start:** read `context.md` before doing any work
- **During a session:** update `context.md` after any meaningful action (new files, decisions, features, next steps)

## What this app does

Monitors UK rail routes for upcoming disruptions, using the **TfL Unified API** as the sole
data source (issue #24 rewrite — replaced the earlier Google Maps + GTFS dual-source design).
Users define routes as origin → destination stations, identified by TfL StopPoint id (picked
via a live TfL station search — no CRS codes). TfL resolves interchanges itself, so a route is
scanned as one full origin→destination journey, not per-leg. The app captures a baseline
itinerary at noon for each direction (outbound and return), then a weekly scheduler scans
upcoming dates and compares the actual itinerary structure against the baseline to flag
disruptions.

## Stack

- **FastAPI** (Python) — web server and REST API
- **APScheduler** — weekly background scan cron job
- **SQLite** (WAL mode) — single-file database at `DB_PATH` (default `/data/rail.db`)
- **Docker Compose** — single container, named volume for persistence
- **Jinja2 + vanilla JS** — server-rendered templates with lightweight frontend

## File map

```
app/
  main.py          — FastAPI app init, lifespan hooks (init_db, setup_scheduler)
  database.py      — SQLite connection factory and schema init (init_db)
  models.py        — Pydantic request models (StopPoint, RouteCreate/Update, BaselineConfirm)
  scanner.py       — Core disruption detection logic (fetch_baseline_options, confirm_baseline, scan_route, scan_all_routes)
  scheduler.py     — APScheduler setup; reads SCAN_DOW/SCAN_HOUR/SCAN_MINUTE env vars
  tfl_client.py    — TfL Unified API client (search_stop_points, stop_point_exists, fetch_journeys, fetch_journeys_at)
  display.py       — route_display_name(), route_direction_labels()
  routers/
    admin.py       — Route CRUD, baseline capture/confirm, manual scan trigger, station search proxy
    kiosk.py       — Kiosk view data (shared _build_route_data helper with reports)
    reports.py     — Calendar data, per-day breakdown, _derive_issues
  templates/       — Jinja2 HTML templates
  static/          — app.js, style.css
```

## Database schema

Four tables, created in `init_db()`.

- **routes** — user-defined routes: `name`, `origin_stop_id`/`origin_name`, `destination_stop_id`/`destination_name` (TfL StopPoint ids, no CRS), `scan_days`, `lookahead_weeks`, `threshold_pct`, `kiosk_visible`, `kiosk_color`, `last_scanned_at`
- **baselines** — one row per `(route_id, direction)`: `origin_stop_id`/`destination_stop_id` (snapshotted at capture time), `duration_s`, `interchange_stops` (JSON, ordered naptanIds), `leg_modes` (JSON), `steps` (JSON, normalized display steps)
- **scan_results** — one row per `(route_id, target_date, direction)`: `status` = NORMAL/DISRUPTED/UNKNOWN, `duration_s`, `matched_steps` (JSON, present iff NORMAL), `alternate_steps` (JSON, present iff DISRUPTED — the fastest itinerary actually found), `disruption_reasons` (JSON), `calls_made`, `window_fully_walked`
- **api_usage_log** — every TfL API call logged with `route_id` and `purpose`; `route_id` is SET NULL (not cascaded) on route deletion

## Core scanning logic (`scanner.py`)

### Baseline capture (two-step)
`fetch_baseline_options(route_id, baseline_date)` — one TfL query per direction at noon, returns the (up to 3) returned itineraries for the admin to review.

`confirm_baseline(route_id, baseline_date, outbound, return_)` — transactionally saves the admin-selected itinerary for both directions, snapshotting the route's current stop ids.

### Weekly scan
`scan_route(route_id)` — for each target date in the lookahead window matching `scan_days`, calls `_scan_direction` for each direction.

`_scan_direction(route, baseline, target_date, direction)` — itinerary-match detection, not duration-only. Queries TfL at noon; if none of the returned itineraries structurally match the baseline (same ordered interchange stations, same leg modes, duration within threshold), pages forward via a max-departure re-query (never TfL's `later.uri`) up to an 8-call cap within a 2-hour window. A mid-window API error or reaching the call cap without confirming the full window → `UNKNOWN` (never a confident `DISRUPTED` off an incomplete picture). A confirmed empty window → `DISRUPTED`, storing the fastest itinerary actually seen as `alternate_steps`.

`scan_all_routes()` runs `scan_route` for every route in the DB, called by the scheduler and also triggerable via `POST /api/scan-all`.

## TfL Unified API (`tfl_client.py`)

- `search_stop_points(query)` — proxies `GET /StopPoint/Search/{q}`, filtered to rail/tube/DLR/overground/tram/Elizabeth line modes. Used by the admin route form's live search.
- `stop_point_exists(id)` — `GET /StopPoint/{id}` existence check at route-creation time.
- `fetch_journeys`/`fetch_journeys_at` — `GET /Journey/JourneyResults/{from}/to/{to}`, deliberately **without** `mode=`/`journeyPreference=` params (those would filter out the bus/replacement-bus legs detection depends on). 404 → treated as "no data" (only expected past TfL's ~104-day lookahead horizon), never a genuine disruption signal.
- Walking legs are excluded from interchange/mode comparisons (TfL itemizes them inconsistently); a leg's `naptanId` (not `id`, which is null on real responses) is the station identifier used throughout.
- HUB StopPoint ids (multi-modal groups like Waterloo) are rejected — `JourneyResults` returns HTTP 300 for them.
- Rate limit: `TFL_API_KEY`, 500 req/min tier, shared with the `roost`/`tfl-api`/`house-tracker` sibling projects.

## Environment variables

| Variable | Where set | Notes |
|---|---|---|
| `TFL_API_KEY` | `.env` | Required |
| `DB_PATH` | `docker-compose.yml` | Defaults to `/data/rail.db` in code |
| `SCAN_DOW` | `docker-compose.yml` | APScheduler day_of_week string, default `sun` |
| `SCAN_HOUR` | `docker-compose.yml` | Integer, default `6` |
| `SCAN_MINUTE` | `docker-compose.yml` | Integer, default `0` |

`docker-compose.yml` `environment:` block takes precedence over `env_file:` (`.env`) for the same key.

## Key design decisions

- **No ORM** — raw `sqlite3` with `row_factory = sqlite3.Row`. Every DB interaction opens and closes its own connection.
- **Synchronous scanner** — scanner runs in a thread pool executor when triggered via the async API endpoint (`POST /api/scan-all`). The scheduler runs it directly in a background thread.
- **Baseline is per-direction, not per-leg** — one baseline row per `(route_id, direction)`. TfL resolves interchanges itself, so there's no separate leg1/leg2 baseline.
- **scan_results are upserted** — `INSERT OR REPLACE` on `(route_id, target_date, direction)`, so re-scanning a date overwrites previous results.
- **Single noon scan, paginated forward on mismatch** — one call at noon; only pages forward (up to 8 calls, 2-hour window) if the first page doesn't structurally match the baseline.
- **Structural detection, not text-driven** — `disruptions[]`/`plannedWorks[]` text never drives NORMAL/DISRUPTED status (too noisy — TfL surfaces a blanket "Information"-category advisory on nearly every Clapham Junction-routed itinerary regardless of actual disruption). Detection is purely structural: interchange sequence, leg modes, duration threshold.
- **Bidirectional** — both outbound and return are always scanned.
- **Exactly 2 fixed routes today, general UI still kept** — the admin route CRUD stays general (not hardcoded to 2), but only Barnes→Waterloo and Barnes→Guildford are currently configured.
