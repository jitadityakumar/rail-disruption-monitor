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
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS routes (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                TEXT NOT NULL,
                origin_stop_id      TEXT NOT NULL,
                origin_name         TEXT NOT NULL,
                destination_stop_id TEXT NOT NULL,
                destination_name    TEXT NOT NULL,
                departure_time      TEXT NOT NULL DEFAULT '08:00',
                return_time         TEXT NOT NULL DEFAULT '18:00',
                threshold_pct       INTEGER NOT NULL DEFAULT 20,
                kiosk_visible       INTEGER NOT NULL DEFAULT 1,
                kiosk_color         TEXT NOT NULL DEFAULT 'blue',
                last_scanned_at     TEXT,
                created_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS baselines (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id              INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
                baseline_date         TEXT NOT NULL,
                direction             TEXT NOT NULL,
                origin_stop_id        TEXT NOT NULL,
                destination_stop_id   TEXT NOT NULL,
                duration_s            INTEGER NOT NULL,
                interchange_stops     TEXT NOT NULL,
                leg_modes             TEXT NOT NULL,
                steps                 TEXT NOT NULL,
                captured_at           TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(route_id, direction)
            );

            CREATE TABLE IF NOT EXISTS scan_results (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id            INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
                target_date         TEXT NOT NULL,
                direction           TEXT NOT NULL,
                status              TEXT NOT NULL,
                duration_s          INTEGER,
                matched_steps       TEXT,
                alternate_steps     TEXT,
                disruption_reasons  TEXT,
                calls_made          INTEGER NOT NULL DEFAULT 1,
                window_fully_walked INTEGER NOT NULL DEFAULT 1,
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
