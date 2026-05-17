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

Monitors UK rail routes for upcoming disruptions. Users define routes as CRS station sequences, capture a "baseline" journey on a normal day, then a weekly scheduler scans upcoming dates and compares results against the baseline to flag disruptions.

## Stack

- **FastAPI** (Python) — web server and REST API
- **APScheduler** — weekly background scan cron job
- **SQLite** (WAL mode) — single-file database at `DB_PATH` (default `/data/rail.db`)
- **Docker Compose** — single container, named volume for persistence
- **Jinja2 + vanilla JS** — server-rendered templates with lightweight frontend

## File map

```
app/
  main.py          — FastAPI app init, lifespan hooks (init_db, load_station_list, setup_scheduler)
  database.py      — SQLite connection factory and schema migrations (init_db)
  models.py        — Pydantic request models
  scanner.py       — Core disruption detection logic (capture_baseline, scan_route, etc.)
  scheduler.py     — APScheduler setup; reads SCAN_DOW/SCAN_HOUR/SCAN_MINUTE env vars
  routes_api.py    — Google Maps Routes API v2 client (compute_route, compute_all_routes)
  stations.py      — CRS → lat/lon lookup; loads stations.json, fetches missing coords from RailData API
  stations.json    — Bundled station list (CRS → display name)
  routers/
    admin.py       — Route CRUD, baseline capture/confirm, manual scan trigger, station search
    display.py     — Kiosk view data
    reports.py     — Scan history and breakdown
  templates/       — Jinja2 HTML templates
  static/          — app.js, style.css
```

## Database schema

Four tables, all created idempotently in `init_db()`:

- **routes** — user-defined routes: `crs_sequence` (JSON array), `scan_days` (comma-separated weekday ints 0=Mon), `lookahead_weeks`, `threshold_pct`, `kiosk_visible`
- **baselines** — one per route: journey duration and transit steps for slots 08:00, 13:00, 18:00 on a chosen baseline date
- **scan_results** — one row per (route, target_date, time_slot): `status` (NORMAL/DISRUPTED/UNKNOWN), `duration_s`, `steps` (JSON), `disruption_reasons` (JSON)
- **api_usage_log** — every Routes API call logged with `route_id` and `purpose`
- **station_coords** — cached CRS → lat/lon, populated lazily via RailData API

## Core scanning logic (`scanner.py`)

### Baseline capture
`capture_baseline(route_id, baseline_date)` — queries Routes API at 08:00, 13:00, 18:00 on the baseline date, stores duration and transit step sequence in `baselines` table.

There is also `fetch_baseline_options` / `confirm_baseline` — a two-step flow where the admin can preview multiple route alternatives (queried at +0/+30/+60 min offsets) and manually confirm which represents normal service.

### Weekly scan
`scan_route(route_id)` — for each target date in the lookahead window matching `scan_days`, calls `_query_and_compare` for each time slot.

`_query_and_compare` — queries the API at up to 4 offsets (0/30/60/90 min) to find the best matching route, then checks three disruption signals:
1. **Rail replacement bus** — a BUS vehicle type appears on a leg that was HEAVY_RAIL/COMMUTER_TRAIN in the baseline
2. **Duration threshold** — journey time exceeds `baseline * (1 + threshold_pct/100)`
3. **Stop sequence change** — `(dep_stop, arr_stop)` pairs differ from baseline sequence

`scan_all_routes()` runs `scan_route` for every route in the DB, called by the scheduler and also triggerable via `POST /api/scan-all`.

## API integrations

### Google Maps Routes API v2 (`routes_api.py`)
- Endpoint: `https://routes.googleapis.com/directions/v2:computeRoutes`
- Travel mode: `TRANSIT` with `FEWER_TRANSFERS` preference
- **Important:** TRANSIT mode does not support intermediate waypoints in Routes API v2. Waypoints in `crs_sequence` are stored for display/monitoring logic only — they are not passed to the API. The API is called with origin and destination only.
- Field mask requests only the fields needed for disruption detection (duration, stop details, vehicle type, line/agency)

### RailData Stations API (`stations.py`)
- Used to resolve CRS codes to lat/lon coordinates
- Results are cached in the `station_coords` table; only fetched if not already cached
- Key passed via `RAILDATA_STATIONS_API_KEY` env var

## Environment variables

| Variable | Where set | Notes |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | `.env` | Required |
| `RAILDATA_STATIONS_API_KEY` | `.env` | Required |
| `DB_PATH` | `docker-compose.yml` | Defaults to `/data/rail.db` in code |
| `SCAN_DOW` | `docker-compose.yml` | APScheduler day_of_week string, default `sun` |
| `SCAN_HOUR` | `docker-compose.yml` | Integer, default `6` |
| `SCAN_MINUTE` | `docker-compose.yml` | Integer, default `0` |

`docker-compose.yml` `environment:` block takes precedence over `env_file:` (`.env`) for the same key.

## Key design decisions

- **No ORM** — raw `sqlite3` with `row_factory = sqlite3.Row`. Every DB interaction opens and closes its own connection.
- **Synchronous scanner** — scanner runs in a thread pool executor when triggered via the async API endpoint (`POST /api/scan-all`). The scheduler runs it directly in a background thread.
- **Baseline is per-route, not per-date** — only one baseline is stored per route (`UNIQUE(route_id)` constraint). Capturing a new baseline overwrites the old one.
- **scan_results are upserted** — `INSERT OR REPLACE` on `(route_id, target_date, time_slot)`, so re-scanning a date overwrites previous results.
