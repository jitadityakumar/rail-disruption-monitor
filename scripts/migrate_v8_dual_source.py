"""
Migration v8: dual-source columns for routes/baselines/scan_results (GTFS second signal,
issue #15).

Run once against the live DB before deploying the v8 code:
    docker exec rail-disruption-monitor-app-1 python3 /app/scripts/migrate_v8_dual_source.py

- routes: adds scan_source (default 'maps'), gtfs_lookahead_weeks, gtfs_scan_days.
  Plain ADD COLUMN — no constraint change, existing rows unaffected.
- baselines: adds source (default 'maps') and widens UNIQUE(route_id) to
  UNIQUE(route_id, source). SQLite can't ALTER a UNIQUE constraint, so this table is
  rebuilt: create baselines_new with the new schema, copy rows across (source defaults
  to 'maps'), drop old, rename.
- scan_results: same rebuild, widening UNIQUE(route_id, target_date, direction, leg) to
  include source.

All existing rows end up with source='maps', matching today's Maps-only behavior exactly.
"""
import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/data/rail.db")

ROUTES_NEW_COLS = [
    ("scan_source", "TEXT NOT NULL DEFAULT 'maps'"),
    ("gtfs_lookahead_weeks", "INTEGER"),
    ("gtfs_scan_days", "TEXT"),
]

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA foreign_keys=OFF")

added = []
existing = {r[1] for r in conn.execute("PRAGMA table_info(routes)").fetchall()}
for col, decl in ROUTES_NEW_COLS:
    if col not in existing:
        conn.execute(f"ALTER TABLE routes ADD COLUMN {col} {decl}")
        added.append(f"routes.{col}")

rebuilt = []


def _rebuild_baselines():
    cols = {r[1] for r in conn.execute("PRAGMA table_info(baselines)").fetchall()}
    if "source" in cols:
        return
    conn.execute("DROP TABLE IF EXISTS baselines_new")
    conn.execute("""
        CREATE TABLE baselines_new (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id                    INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
            baseline_date               TEXT NOT NULL,
            outbound_leg1_duration_s    INTEGER,
            outbound_leg1_steps         TEXT,
            outbound_leg1_dep_stop      TEXT,
            outbound_leg1_arr_stop      TEXT,
            outbound_leg2_duration_s    INTEGER,
            outbound_leg2_steps         TEXT,
            outbound_leg2_dep_stop      TEXT,
            outbound_leg2_arr_stop      TEXT,
            return_leg1_duration_s      INTEGER,
            return_leg1_steps           TEXT,
            return_leg1_dep_stop        TEXT,
            return_leg1_arr_stop        TEXT,
            return_leg2_duration_s      INTEGER,
            return_leg2_steps           TEXT,
            return_leg2_dep_stop        TEXT,
            return_leg2_arr_stop        TEXT,
            captured_at                 TEXT NOT NULL DEFAULT (datetime('now')),
            source                      TEXT NOT NULL DEFAULT 'maps',
            UNIQUE(route_id, source)
        )
    """)
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(baselines)").fetchall()]
    col_list = ", ".join(old_cols)
    conn.execute(f"INSERT INTO baselines_new ({col_list}) SELECT {col_list} FROM baselines")
    conn.execute("DROP TABLE baselines")
    conn.execute("ALTER TABLE baselines_new RENAME TO baselines")
    rebuilt.append("baselines")


def _rebuild_scan_results():
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scan_results)").fetchall()}
    if "source" in cols:
        return
    conn.execute("DROP TABLE IF EXISTS scan_results_new")
    conn.execute("""
        CREATE TABLE scan_results_new (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id            INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
            target_date         TEXT NOT NULL,
            direction           TEXT NOT NULL,
            leg                 INTEGER NOT NULL DEFAULT 1,
            status              TEXT NOT NULL,
            duration_s          INTEGER,
            steps               TEXT,
            disruption_reasons  TEXT,
            scanned_at          TEXT NOT NULL DEFAULT (datetime('now')),
            source              TEXT NOT NULL DEFAULT 'maps',
            UNIQUE(route_id, target_date, direction, leg, source)
        )
    """)
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(scan_results)").fetchall()]
    col_list = ", ".join(old_cols)
    conn.execute(
        f"INSERT INTO scan_results_new ({col_list}) SELECT {col_list} FROM scan_results"
    )
    conn.execute("DROP TABLE scan_results")
    conn.execute("ALTER TABLE scan_results_new RENAME TO scan_results")
    rebuilt.append("scan_results")


_rebuild_baselines()
_rebuild_scan_results()

conn.commit()
conn.execute("PRAGMA foreign_keys=ON")
conn.close()

if added:
    print(f"Added columns: {', '.join(added)}")
if rebuilt:
    print(f"Rebuilt tables (added source, widened UNIQUE): {', '.join(rebuilt)}")
if not added and not rebuilt:
    print("All columns/constraints already present — nothing to do.")
