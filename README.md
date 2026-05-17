# Rail Disruption Monitor

A self-hosted tool that monitors UK rail routes for disruptions and flags them before you travel. You define routes by origin, optional interchange, and destination, capture a baseline journey on a normal day, and the app scans upcoming weekends weekly to detect changes — rail replacement buses, significant delays, or trains no longer reaching their destination.

## How it works

1. **Define a route** — specify an origin CRS, optional interchange CRS, and destination CRS (e.g. `BNS → CLJ → LRD`)
2. **Capture a baseline** — the app queries the Google Maps Routes API at noon for each leg (outbound and return), showing journey options; you pick the one that represents normal service
3. **Weekly scan** — every Sunday at 06:00 (by default), the app scans each configured weekday in your lookahead window, querying both outbound and return at noon
4. **Disruption detection** — each leg is scanned independently and marked DISRUPTED if any of the following are true:
   - A rail replacement bus appears on that leg
   - Journey time exceeds baseline by more than a configurable threshold (default 20%)
   - The train no longer reaches the baseline arrival station
5. **Display view** — a kiosk-friendly page shows upcoming disruptions at a glance, and the reports page gives an 8-week calendar per route with per-day detail

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

| Variable | Description | Default |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | Google Maps Routes API key | required |
| `RAILDATA_STATIONS_API_KEY` | RailData Stations API key for CRS → coordinates lookup | required |
| `DB_PATH` | Path to SQLite database inside container | `/data/rail.db` |
| `SCAN_DOW` | APScheduler day-of-week string for weekly scan | `sun` |
| `SCAN_HOUR` | Hour to run the weekly scan | `6` |
| `SCAN_MINUTE` | Minute to run the weekly scan | `0` |

## Secrets

`.env` is gitignored and must **never** be committed. A pre-commit hook (`scripts/check-secrets.sh`) enforces this. Install it once per checkout:

```bash
chmod +x scripts/check-secrets.sh
ln -sf ../../scripts/check-secrets.sh .git/hooks/pre-commit
```

## Security

A weekly automated audit runs via GitHub Actions every Friday, checking Python dependencies for known CVEs using `pip-audit`. If vulnerabilities are found it opens a GitHub issue; if an issue is already open it adds a comment; once the audit is clean it closes the issue automatically.

To trigger the audit manually: **Actions → Security Audit → Run workflow**.

### Marking a vulnerability as accepted risk

If a CVE is not worth fixing, add it to `security/accepted-risks.txt`:

```
CVE-2025-12345  # package: reason — not reachable in production, no fix available. Accepted 2026-05-17.
```

The audit workflow skips any CVEs listed in that file.

## Web interface

| Path | Description |
|---|---|
| `/admin` | Manage routes, capture baselines, trigger manual scans |
| `/kiosk` | Kiosk view — upcoming disruptions across all kiosk-visible routes (max 3), auto-refreshes every 5 minutes |
| `/reports` | 8-week calendar per route with disruption detail and day-level modal |

## Data storage

The SQLite database is stored in a Docker named volume (`db-data`) at `/data/rail.db` inside the container. The physical location on the host is:

```
/var/lib/docker/volumes/rail-disruption-monitor_db-data/_data/rail.db
```

The database is local to the machine — it does not move when you redeploy to a new host. Migrate it manually if needed by copying the `rail.db` file across.
