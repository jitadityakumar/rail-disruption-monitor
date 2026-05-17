# Rail Disruption Monitor

A self-hosted tool that monitors UK rail routes for disruptions and flags them before you travel. You define routes by station sequence, capture a baseline journey on a normal day, and the app scans upcoming dates weekly to detect changes — rail replacement buses, significant delays, or rerouting.

## How it works

1. **Define a route** — specify a sequence of station CRS codes (e.g. `BNS → WTN → WAT`)
2. **Capture a baseline** — the app queries the Google Maps Routes API for that route on a normal service day, recording journey time and transit steps at 08:00, 13:00, and 18:00
3. **Weekly scan** — every Sunday at 06:00 (by default), the app queries the same route for each upcoming date in your lookahead window and compares results against the baseline
4. **Disruption detection** — a date/slot is flagged as disrupted if any of the following are true:
   - A rail replacement bus appears on a leg that was previously a train
   - Journey time exceeds baseline by more than a configurable threshold (default 20%)
   - The stop sequence has changed
5. **Display view** — a kiosk-friendly page shows upcoming disruptions at a glance

## Prerequisites

- Docker and Docker Compose
- A [Google Maps Routes API](https://developers.google.com/maps/documentation/routes) key (with Routes API enabled)
- A [RailData Stations API](https://www.raildata.org.uk/) key (used to look up station coordinates by CRS code)

## Setup

```bash
cp .env.example .env
# Edit .env and fill in your API keys
docker compose up -d
```

The app is available at `http://localhost:8000`.

## Configuration

| Variable | Description |
|---|---|
| `GOOGLE_MAPS_API_KEY` | Google Maps Routes API key |
| `RAILDATA_STATIONS_API_KEY` | RailData Stations API key for CRS → coordinates lookup |

The scan schedule and other runtime settings are configured in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `SCAN_DOW` | `sun` | Day of week to run the weekly scan |
| `SCAN_HOUR` | `6` | Hour to run the scan (24h, Europe/London) |
| `SCAN_MINUTE` | `0` | Minute to run the scan |
| `DB_PATH` | `/data/rail.db` | Path to the SQLite database inside the container |

## Web interface

| Path | Description |
|---|---|
| `/admin` | Manage routes, capture baselines, trigger manual scans |
| `/display` | Kiosk view — upcoming disruptions across all visible routes |
| `/reports` | Detailed scan history and per-date breakdowns |

## Data storage

The SQLite database is stored in a Docker named volume (`db-data`) at `/data/rail.db` inside the container. The physical location on the host is:

```
/var/lib/docker/volumes/rail-disruption-monitor_db-data/_data/rail.db
```

The database is local to the machine — it does not move when you redeploy to a new host. Migrate it manually if needed by copying the `rail.db` file across.
