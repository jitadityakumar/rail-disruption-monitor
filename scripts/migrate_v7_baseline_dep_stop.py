"""
Migration v7: add dep_stop columns to baselines table.

Run once against the live DB before deploying the v7 code:
    docker exec rail-disruption-monitor-app-1 python3 /app/scripts/migrate_v7_baseline_dep_stop.py

Existing baseline rows will have NULL dep_stop; the scanner will skip the
dep_stop check for those legs until the baseline is re-captured.
"""
import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/data/rail.db")

NEW_COLS = [
    "outbound_leg1_dep_stop",
    "outbound_leg2_dep_stop",
    "return_leg1_dep_stop",
    "return_leg2_dep_stop",
]

conn = sqlite3.connect(DB_PATH)
existing = {r[1] for r in conn.execute("PRAGMA table_info(baselines)").fetchall()}
added = []
for col in NEW_COLS:
    if col not in existing:
        conn.execute(f"ALTER TABLE baselines ADD COLUMN {col} TEXT")
        added.append(col)
conn.commit()
conn.close()

if added:
    print(f"Added columns: {', '.join(added)}")
else:
    print("All columns already present — nothing to do.")
