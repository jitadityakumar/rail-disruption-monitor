import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

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
    origin_crs="BNS",
    change_crs=None,
    destination_crs="WAT",
    scan_days="5,6",
    lookahead_weeks=4,
    threshold_pct=20,
    scan_source="maps",
    gtfs_lookahead_weeks=None,
    gtfs_scan_days=None,
) -> int:
    conn = db.get_db()
    try:
        cur = conn.execute(
            """INSERT INTO routes
               (name, origin_crs, change_crs, destination_crs, scan_days, lookahead_weeks,
                threshold_pct, kiosk_visible, scan_source, gtfs_lookahead_weeks, gtfs_scan_days)
               VALUES ('Test Route', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (origin_crs, change_crs, destination_crs, scan_days, lookahead_weeks,
             threshold_pct, scan_source, gtfs_lookahead_weeks, gtfs_scan_days),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def insert_maps_baseline(db, route_id: int, *, arr_stop="WAT", dep_stop="BNS", duration_s=1800):
    conn = db.get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO baselines
               (route_id, baseline_date, source,
                outbound_leg1_duration_s, outbound_leg1_steps, outbound_leg1_dep_stop, outbound_leg1_arr_stop,
                return_leg1_duration_s, return_leg1_steps, return_leg1_dep_stop, return_leg1_arr_stop)
               VALUES (?, '2026-08-01', 'maps', ?, '[]', ?, ?, ?, '[]', ?, ?)""",
            (route_id, duration_s, dep_stop, arr_stop, duration_s, arr_stop, dep_stop),
        )
        conn.commit()
    finally:
        conn.close()


def insert_gtfs_baseline(
    db,
    route_id: int,
    *,
    origin_crs="BNS",
    dest_crs="WAT",
    trip_count_by_weekday=None,
    duration_s=1800,
    only_outbound_leg1=False,
):
    trip_count_by_weekday = trip_count_by_weekday if trip_count_by_weekday is not None else {"5": 10, "6": 6}
    steps = __import__("json").dumps({
        "trip_count_by_weekday": trip_count_by_weekday,
        "departure_time": "12:00",
        "intermediate_stops": [],
    })
    conn = db.get_db()
    try:
        if only_outbound_leg1:
            conn.execute(
                """INSERT OR REPLACE INTO baselines
                   (route_id, baseline_date, source,
                    outbound_leg1_duration_s, outbound_leg1_steps, outbound_leg1_dep_stop, outbound_leg1_arr_stop)
                   VALUES (?, '2026-08-01', 'gtfs', ?, ?, ?, ?)""",
                (route_id, duration_s, steps, origin_crs, dest_crs),
            )
        else:
            conn.execute(
                """INSERT OR REPLACE INTO baselines
                   (route_id, baseline_date, source,
                    outbound_leg1_duration_s, outbound_leg1_steps, outbound_leg1_dep_stop, outbound_leg1_arr_stop,
                    return_leg1_duration_s, return_leg1_steps, return_leg1_dep_stop, return_leg1_arr_stop)
                   VALUES (?, '2026-08-01', 'gtfs', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (route_id, duration_s, steps, origin_crs, dest_crs,
                 duration_s, steps, dest_crs, origin_crs),
            )
        conn.commit()
    finally:
        conn.close()


def get_scan_results(db, route_id: int, source: str | None = None):
    conn = db.get_db()
    try:
        if source:
            return conn.execute(
                "SELECT * FROM scan_results WHERE route_id=? AND source=? ORDER BY target_date, direction, leg",
                (route_id, source),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM scan_results WHERE route_id=? ORDER BY target_date, direction, leg, source",
            (route_id,),
        ).fetchall()
    finally:
        conn.close()
