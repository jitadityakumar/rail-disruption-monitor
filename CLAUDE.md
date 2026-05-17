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

Monitors UK rail routes for upcoming disruptions. Users define routes as origin → (optional interchange) → destination CRS codes. Each leg is scanned independently: the app captures a baseline at noon for each direction (outbound and return), then a weekly scheduler scans upcoming weekends and compares each leg against the baseline to flag disruptions.

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
  database.py      — SQLite connection factory and schema init (init_db, purely additive CREATE IF NOT EXISTS)
  models.py        — Pydantic request models
  scanner.py       — Core disruption detection logic (fetch_baseline_options, confirm_baseline, scan_route, scan_all_routes)
  scheduler.py     — APScheduler setup; reads SCAN_DOW/SCAN_HOUR/SCAN_MINUTE env vars
  routes_api.py    — Google Maps Routes API v2 client (compute_route, compute_all_routes)
  stations.py      — CRS → lat/lon lookup, route_display_name(), route_leg_labels(), leg_label()
  stations.json    — Bundled station list (CRS → display name)
  routers/
    admin.py       — Route CRUD, baseline capture/confirm, manual scan trigger, station search
    kiosk.py       — Kiosk view data (shared _build_route_data helper with reports)
    reports.py     — 8-week calendar data and per-day breakdown
  templates/       — Jinja2 HTML templates
  static/          — app.js, style.css
```

## Database schema

Five tables, all created idempotently in `init_db()` using `CREATE TABLE IF NOT EXISTS`. Schema migrations are additive `ALTER TABLE` scripts in `scripts/`.

- **routes** — user-defined routes: `name`, `origin_crs`, `change_crs` (nullable), `destination_crs`, `scan_days` (comma-separated weekday ints 0=Mon), `lookahead_weeks`, `threshold_pct`, `kiosk_visible`, `last_scanned_at`
- **baselines** — one per route (`UNIQUE(route_id)`): per-leg columns for outbound and return directions — `outbound_leg1_duration_s/steps/dep_stop/arr_stop`, `outbound_leg2_*`, `return_leg1_*`, `return_leg2_*`. Leg 2 columns are NULL for routes with no interchange.
- **scan_results** — one row per `(route_id, target_date, direction, leg)`: `direction` = `outbound`/`return`, `leg` = 1 or 2, `status` = NORMAL/DISRUPTED/UNKNOWN, `duration_s`, `steps` (JSON), `disruption_reasons` (JSON)
- **api_usage_log** — every Routes API call logged with `route_id` and `purpose`; `route_id` is SET NULL (not cascaded) on route deletion
- **station_coords** — cached CRS → lat/lon, populated lazily via RailData API

## Core scanning logic (`scanner.py`)

### Baseline capture (two-step)
`fetch_baseline_options(route_id, baseline_date)` — queries each leg at noon (+0/+30/+60 min, with `computeAlternativeRoutes: True`), deduplicates options by stop-sequence fingerprint, and returns them for the admin to review.

`confirm_baseline(route_id, baseline_date, selections)` — saves the admin-selected option per direction+leg into the `baselines` table (`dep_stop`, `arr_stop`, `duration_s`, `steps`).

### Weekly scan
`scan_route(route_id)` — for each target date in the lookahead window matching `scan_days`, calls `_query_and_compare` for every direction+leg combination derived from the stored baseline.

`_query_and_compare(origin_crs, dest_crs, baseline_dep_stop, baseline_arr_stop, ...)` — queries the API at up to 5 departure offsets from noon (0/30/60/90/120 min), at each calling `compute_all_routes` (alternatives=True). Stops as soon as any option matches the baseline `dep_stop` and `arr_stop`; uses first result as fallback. Checks three disruption signals:
1. **Rail replacement bus** — a single direct BUS step matching baseline dep+arr (multi-step routes ignored)
2. **Duration threshold** — journey time exceeds `baseline * (1 + threshold_pct/100)`
3. **Wrong destination** — train no longer reaches the baseline `arr_stop`

`scan_all_routes()` runs `scan_route` for every route in the DB, called by the scheduler and also triggerable via `POST /api/scan-all`.

## API integrations

### Google Maps Routes API v2 (`routes_api.py`)
- Endpoint: `https://routes.googleapis.com/directions/v2:computeRoutes`
- Travel mode: `TRANSIT` with `FEWER_TRANSFERS` preference
- **Important:** TRANSIT mode does not support intermediate waypoints in Routes API v2. Each leg (origin→interchange, interchange→destination) is queried as a separate origin-to-destination call with no waypoints.
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
- **scan_results are upserted** — `INSERT OR REPLACE` on `(route_id, target_date, direction, leg)`, so re-scanning a date overwrites previous results.
- **Single noon scan** — engineering works are full-day, so one scan at noon per direction per leg is sufficient. Reduces API usage vs the old 08:00/13:00/18:00 approach.
- **Leg-based scanning** — each leg (e.g. BNS→CLJ, CLJ→LRD) is queried and reported independently. A disruption on leg 2 doesn't mask a normal leg 1.
- **Bidirectional** — both outbound and return are always scanned; the return is the reversed CRS sequence.
- **dep_stop validation** — baseline stores the expected departure station per leg; a scan result departing from a different station (e.g. Guildford instead of LRD) is rejected as "no direct service found".
