import json
import os
import sys
from pathlib import Path

import pytest

_APP_DIR = Path(__file__).parent.parent / "app"
sys.path.insert(0, str(_APP_DIR))
os.chdir(_APP_DIR)  # main.py mounts "static"/"templates" as paths relative to cwd

import database  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    database.init_db()
    return database


def insert_route(
    db,
    *,
    origin_stop_id="910GBARNES",
    origin_name="Barnes",
    destination_stop_id="910GWATRLMN",
    destination_name="London Waterloo",
    scan_days="5,6",
    lookahead_weeks=4,
    threshold_pct=20,
    kiosk_visible=1,
    kiosk_color="blue",
) -> int:
    conn = db.get_db()
    try:
        cur = conn.execute(
            """INSERT INTO routes
               (name, origin_stop_id, origin_name, destination_stop_id, destination_name,
                scan_days, lookahead_weeks, threshold_pct, kiosk_visible, kiosk_color)
               VALUES ('Test Route', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (origin_stop_id, origin_name, destination_stop_id, destination_name,
             scan_days, lookahead_weeks, threshold_pct, kiosk_visible, kiosk_color),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def insert_baseline(
    db,
    route_id: int,
    *,
    direction="outbound",
    origin_stop_id="910GBARNES",
    destination_stop_id="910GWATRLMN",
    duration_s=1800,
    interchange_stops=None,
    leg_modes=None,
    steps=None,
) -> None:
    interchange_stops = interchange_stops if interchange_stops is not None else []
    leg_modes = leg_modes if leg_modes is not None else ["national-rail"]
    steps = steps if steps is not None else []
    conn = db.get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO baselines
               (route_id, baseline_date, direction, origin_stop_id, destination_stop_id,
                duration_s, interchange_stops, leg_modes, steps)
               VALUES (?, '2026-08-01', ?, ?, ?, ?, ?, ?, ?)""",
            (route_id, direction, origin_stop_id, destination_stop_id, duration_s,
             json.dumps(interchange_stops), json.dumps(leg_modes), json.dumps(steps)),
        )
        conn.commit()
    finally:
        conn.close()


def insert_both_baselines(db, route_id: int, *, origin_stop_id="910GBARNES",
                           destination_stop_id="910GWATRLMN", duration_s=1800,
                           interchange_stops=None, leg_modes=None, steps=None) -> None:
    insert_baseline(
        db, route_id, direction="outbound", origin_stop_id=origin_stop_id,
        destination_stop_id=destination_stop_id, duration_s=duration_s,
        interchange_stops=interchange_stops, leg_modes=leg_modes, steps=steps,
    )
    insert_baseline(
        db, route_id, direction="return", origin_stop_id=destination_stop_id,
        destination_stop_id=origin_stop_id, duration_s=duration_s,
        interchange_stops=interchange_stops, leg_modes=leg_modes, steps=steps,
    )


def get_scan_results(db, route_id: int):
    conn = db.get_db()
    try:
        return conn.execute(
            "SELECT * FROM scan_results WHERE route_id=? ORDER BY target_date, direction",
            (route_id,),
        ).fetchall()
    finally:
        conn.close()
