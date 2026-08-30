"""
Migration v9: TfL-only rewrite (issue #24). Drop-and-recreate -- clean-slate DB wipe, no
migration path from the old Maps/GTFS dual-source schema (confirmed decision, issue #24
comment thread).

Run once, immediately as part of the deploy step, BEFORE the new container serves its first
request (not as a separate later step -- v1 of this plan wiped after confirming deploy
healthy, which left a window where new code ran against the old schema and 500'd on every
DB-touching endpoint).

deploy.sh builds the image from the app/ directory only, so scripts/ (this file included)
is never copied into the container -- `docker exec ... /app/scripts/...` will 404. Ship this
file to the target host and into the running container manually instead (confirmed working
against jk-server-gb, 2026-08-31):
    scp scripts/migrate_v9_tfl_rewrite.py REMOTE:/tmp/migrate_v9_tfl_rewrite.py
    ssh REMOTE docker cp /tmp/migrate_v9_tfl_rewrite.py rail-disruption-monitor-app-1:/app/migrate_v9_tfl_rewrite.py
    ssh REMOTE docker exec rail-disruption-monitor-app-1 python3 /app/migrate_v9_tfl_rewrite.py
    ssh REMOTE "docker exec rail-disruption-monitor-app-1 rm /app/migrate_v9_tfl_rewrite.py; rm /tmp/migrate_v9_tfl_rewrite.py"

Drops routes, baselines, scan_results, station_coords, api_usage_log entirely and recreates
them via database.init_db()'s current schema. Confirm explicitly with the user before running
against a production DB.
"""
import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "/data/rail.db")

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript("""
            DROP TABLE IF EXISTS scan_results;
            DROP TABLE IF EXISTS baselines;
            DROP TABLE IF EXISTS routes;
            DROP TABLE IF EXISTS station_coords;
            DROP TABLE IF EXISTS api_usage_log;
        """)
        conn.commit()
    finally:
        conn.close()

    import database
    database.init_db()
    print(f"Migration v9 complete: {DB_PATH} wiped and recreated with the TfL-only schema.")


if __name__ == "__main__":
    main()
