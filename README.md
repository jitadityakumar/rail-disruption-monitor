# Rail Disruption Monitor

A self-hosted tool that monitors UK rail routes for disruptions and flags them before you
travel. You define routes by origin and destination station (picked via live TfL search),
capture a baseline journey on a normal day, and the app scans upcoming dates weekly to detect
changes — rail replacement buses, a diverted/interchange route, or a significant delay.

## How it works

1. **Define a route** — search for an origin and destination station via TfL's own station
   search (e.g. Barnes → London Waterloo); TfL resolves any interchange itself, no manual
   "change at" station needed
2. **Capture a baseline** — the app queries the TfL Unified API at noon for each direction
   (outbound and return), showing the returned itineraries; you pick the one that represents
   normal service
3. **Weekly scan** — every Sunday at 06:00 (by default), the app scans each configured weekday
   in your lookahead window, querying both outbound and return at noon
4. **Disruption detection** — a date is marked DISRUPTED if no itinerary TfL returns
   structurally matches the baseline: same ordered interchange station(s), every leg still its
   expected transit mode (a leg becoming a rail replacement bus fails the match even if timing
   otherwise lines up), and duration within a configurable threshold (default 20%) — never
   driven by TfL's disruption-text fields, which are too noisy on their own
5. **Display view** — a kiosk-friendly page shows upcoming disruptions at a glance, with
   permanent per-route colour identity, and the reports page gives a calendar per route with
   per-day detail, including the actual alternate route found on a disrupted day

## Prerequisites

- Docker and Docker Compose
- A [TfL API](https://api-portal.tfl.gov.uk/) registered key (free, 500 req/min with
  registration vs. 50 req/min anonymous)

## Setup

```bash
cp .env.example .env
# Edit .env and fill in your TfL API key
docker compose up -d
```

The app is available at `http://localhost:8000`.

## Configuration

| Variable | Description | Default |
|---|---|---|
| `TFL_API_KEY` | TfL Unified API registered key | required |
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
| `/admin` | Manage routes (with live TfL station search), capture baselines, trigger manual scans |
| `/kiosk` | Kiosk view — upcoming disruptions across all kiosk-visible routes (max 3), each with a permanent identity colour, auto-refreshes every 5 minutes |
| `/reports` | Calendar per route with disruption detail, alternate-route display, and day-level modal |

## Data storage

The SQLite database is stored in a Docker named volume (`db-data`) at `/data/rail.db` inside the container. The physical location on the host is:

```
/var/lib/docker/volumes/rail-disruption-monitor_db-data/_data/rail.db
```

The database is local to the machine — it does not move when you redeploy to a new host. Migrate it manually if needed by copying the `rail.db` file across.
