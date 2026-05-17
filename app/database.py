import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/data/rail.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_db()
    try:
        # Migrate old 3-slot schema to bidirectional noon-scan schema
        scan_cols = {r[1] for r in conn.execute("PRAGMA table_info(scan_results)").fetchall()}
        if "time_slot" in scan_cols:
            conn.execute("DROP TABLE IF EXISTS scan_results")
        baseline_cols = {r[1] for r in conn.execute("PRAGMA table_info(baselines)").fetchall()}
        if "slot_08_duration_s" in baseline_cols:
            conn.execute("DROP TABLE IF EXISTS baselines")
        conn.commit()

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS routes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                crs_sequence    TEXT NOT NULL,
                scan_days       TEXT NOT NULL DEFAULT '5,6',
                lookahead_weeks INTEGER NOT NULL DEFAULT 4,
                threshold_pct   INTEGER NOT NULL DEFAULT 20,
                kiosk_visible   INTEGER NOT NULL DEFAULT 1,
                last_scanned_at TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS station_coords (
                crs         TEXT PRIMARY KEY,
                latitude    REAL NOT NULL,
                longitude   REAL NOT NULL,
                fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS baselines (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id            INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
                baseline_date       TEXT NOT NULL,
                outbound_duration_s INTEGER,
                outbound_steps      TEXT,
                return_duration_s   INTEGER,
                return_steps        TEXT,
                captured_at         TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(route_id)
            );

            CREATE TABLE IF NOT EXISTS scan_results (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id            INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
                target_date         TEXT NOT NULL,
                direction           TEXT NOT NULL,
                status              TEXT NOT NULL,
                duration_s          INTEGER,
                steps               TEXT,
                disruption_reasons  TEXT,
                scanned_at          TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(route_id, target_date, direction)
            );

            CREATE TABLE IF NOT EXISTS api_usage_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                called_at   TEXT NOT NULL DEFAULT (datetime('now')),
                route_id    INTEGER REFERENCES routes(id) ON DELETE SET NULL,
                purpose     TEXT NOT NULL
            );
        """)
        conn.commit()
    finally:
        conn.close()
