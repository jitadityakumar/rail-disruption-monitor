import datetime
import json

import pytest

import gtfs_api
import scanner
from conftest import get_scan_results, insert_gtfs_baseline, insert_maps_baseline, insert_route

TODAY = datetime.date.today()
TODAY_WD = TODAY.weekday()


def _fake_maps_route(dep: str, arr: str, vehicle_type: str, duration_s: int) -> dict:
    return {
        "duration": f"{duration_s}s",
        "legs": [{
            "steps": [{
                "travelMode": "TRANSIT",
                "transitDetails": {
                    "stopDetails": {
                        "departureStop": {"name": dep},
                        "arrivalStop": {"name": arr},
                    },
                    "transitLine": {"vehicle": {"type": vehicle_type}, "name": "X", "agencies": [{"name": "SWR"}]},
                },
            }],
        }],
    }


def _gtfs_journey(kind="direct", duration_minutes=30, departure_time="12:00", intermediate_stops=None):
    return {
        "kind": kind,
        "departure_time": departure_time,
        "duration_minutes": duration_minutes,
        "direct": {"intermediate_stops": intermediate_stops or []},
    }


def _stub_gtfs_journeys(monkeypatch, result: gtfs_api.GtfsResult):
    monkeypatch.setattr(gtfs_api, "fetch_gtfs_journeys", lambda *a, **k: result)


def _stub_gtfs_direct_trips(monkeypatch, result: gtfs_api.GtfsResult):
    monkeypatch.setattr(gtfs_api, "fetch_gtfs_direct_trips", lambda *a, **k: result)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(scanner.time, "sleep", lambda *_: None)


def _use_single_scan_day(db, route_id, weekday=None):
    """Pin scan_days to just today's weekday so tests only touch one target date."""
    wd = TODAY_WD if weekday is None else weekday
    conn = db.get_db()
    try:
        conn.execute("UPDATE routes SET scan_days=?, lookahead_weeks=1 WHERE id=?", (str(wd), route_id))
        conn.commit()
    finally:
        conn.close()


def _use_single_gtfs_scan_day(db, route_id, weekday=None):
    wd = TODAY_WD if weekday is None else weekday
    conn = db.get_db()
    try:
        conn.execute(
            "UPDATE routes SET gtfs_scan_days=?, gtfs_lookahead_weeks=1 WHERE id=?", (str(wd), route_id)
        )
        conn.commit()
    finally:
        conn.close()


# 1. Maps-only path unchanged
def test_scan_route_maps_only_unchanged(db, monkeypatch):
    route_id = insert_route(db, scan_source="maps")
    insert_maps_baseline(db, route_id, dep_stop="BNS", arr_stop="WAT", duration_s=1800)
    _use_single_scan_day(db, route_id)

    monkeypatch.setattr(scanner, "get_coords", lambda crs: (0.0, 0.0))
    monkeypatch.setattr(
        scanner, "compute_all_routes",
        lambda *a, **k: [_fake_maps_route("BNS", "WAT", "HEAVY_RAIL", 1800)],
    )

    result = scanner.scan_route(route_id)
    assert result["counts"]["NORMAL"] >= 1
    rows = get_scan_results(db, route_id, source="maps")
    assert all(r["source"] == "maps" for r in rows)
    assert any(r["status"] == "NORMAL" for r in rows)


# 2. Missing GTFS trip -> DISRUPTED
def test_scan_route_gtfs_missing_trip(db, monkeypatch):
    route_id = insert_route(db, scan_source="gtfs")
    insert_gtfs_baseline(db, route_id)
    _use_single_gtfs_scan_day(db, route_id)

    _stub_gtfs_journeys(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[]))
    _stub_gtfs_direct_trips(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[]))

    scanner.scan_route(route_id)
    rows = get_scan_results(db, route_id, source="gtfs")
    assert rows
    assert all(r["status"] == "DISRUPTED" for r in rows)
    assert all("no scheduled service found (GTFS)" in json.loads(r["disruption_reasons"]) for r in rows)


# 3. Reduced service -> DISRUPTED
def test_scan_route_gtfs_reduced_service(db, monkeypatch):
    route_id = insert_route(db, scan_source="gtfs")
    insert_gtfs_baseline(db, route_id, trip_count_by_weekday={str(TODAY_WD): 10})
    _use_single_gtfs_scan_day(db, route_id)

    _stub_gtfs_journeys(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[_gtfs_journey()]))
    _stub_gtfs_direct_trips(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[{}] * 3))  # 3 < 10*0.5

    scanner.scan_route(route_id)
    rows = get_scan_results(db, route_id, source="gtfs")
    assert all(r["status"] == "DISRUPTED" for r in rows)
    assert all("Only 3 scheduled trip(s) vs baseline 10" in r["disruption_reasons"] for r in rows)


# 4. Normal -> NORMAL
def test_scan_route_gtfs_normal(db, monkeypatch):
    route_id = insert_route(db, scan_source="gtfs")
    insert_gtfs_baseline(db, route_id, trip_count_by_weekday={str(TODAY_WD): 10})
    _use_single_gtfs_scan_day(db, route_id)

    _stub_gtfs_journeys(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[_gtfs_journey()]))
    _stub_gtfs_direct_trips(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[{}] * 10))

    scanner.scan_route(route_id)
    rows = get_scan_results(db, route_id, source="gtfs")
    assert all(r["status"] == "NORMAL" for r in rows)


# 5. GTFS API error -> UNKNOWN, never DISRUPTED
def test_scan_route_gtfs_api_error(db, monkeypatch):
    route_id = insert_route(db, scan_source="gtfs")
    insert_gtfs_baseline(db, route_id)
    _use_single_gtfs_scan_day(db, route_id)

    _stub_gtfs_journeys(monkeypatch, gtfs_api.GtfsResult(ok=False, error="network_error"))
    _stub_gtfs_direct_trips(monkeypatch, gtfs_api.GtfsResult(ok=False, error="network_error"))

    scanner.scan_route(route_id)
    rows = get_scan_results(db, route_id, source="gtfs")
    assert all(r["status"] == "UNKNOWN" for r in rows)


# 6. Beyond feed coverage -> UNKNOWN
def test_scan_route_gtfs_beyond_feed_coverage(db, monkeypatch):
    route_id = insert_route(db, scan_source="gtfs", gtfs_lookahead_weeks=0)
    insert_gtfs_baseline(db, route_id)
    conn = db.get_db()
    try:
        # weekday far enough out that lookahead_weeks=0 still puts the target date past "today"
        far_wd = (TODAY_WD + 6) % 7
        conn.execute(
            "UPDATE routes SET gtfs_scan_days=?, gtfs_lookahead_weeks=1 WHERE id=?",
            (str(far_wd), route_id),
        )
        conn.commit()
    finally:
        conn.close()

    _stub_gtfs_journeys(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[]))
    _stub_gtfs_direct_trips(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[]))

    # Force the coverage check to say "beyond coverage" regardless of actual date math.
    monkeypatch.setattr(scanner, "_within_gtfs_feed_coverage", lambda *a, **k: False)

    scanner.scan_route(route_id)
    rows = get_scan_results(db, route_id, source="gtfs")
    assert rows
    assert all(r["status"] == "UNKNOWN" for r in rows)
    assert all("date beyond GTFS feed coverage" in json.loads(r["disruption_reasons"]) for r in rows)


# 7. 'both' writes two distinct source rows for same (route,date,direction,leg)
def test_scan_route_both_writes_two_rows(db, monkeypatch):
    route_id = insert_route(db, scan_source="both")
    insert_maps_baseline(db, route_id)
    insert_gtfs_baseline(db, route_id, trip_count_by_weekday={str(TODAY_WD): 5})
    _use_single_scan_day(db, route_id)
    _use_single_gtfs_scan_day(db, route_id)

    monkeypatch.setattr(scanner, "get_coords", lambda crs: (0.0, 0.0))
    monkeypatch.setattr(
        scanner, "compute_all_routes",
        lambda *a, **k: [_fake_maps_route("BNS", "WAT", "HEAVY_RAIL", 1800)],
    )
    _stub_gtfs_journeys(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[_gtfs_journey()]))
    _stub_gtfs_direct_trips(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[{}] * 5))

    scanner.scan_route(route_id)
    rows = get_scan_results(db, route_id)
    sources = {r["source"] for r in rows}
    assert sources == {"maps", "gtfs"}


# 8. 'both' with no GTFS baseline at all -> GTFS rows UNKNOWN, Maps scans normally, no exception
def test_scan_route_both_no_gtfs_baseline_writes_unknown(db, monkeypatch):
    route_id = insert_route(db, scan_source="both")
    insert_maps_baseline(db, route_id)
    _use_single_scan_day(db, route_id)
    _use_single_gtfs_scan_day(db, route_id)

    monkeypatch.setattr(scanner, "get_coords", lambda crs: (0.0, 0.0))
    monkeypatch.setattr(
        scanner, "compute_all_routes",
        lambda *a, **k: [_fake_maps_route("BNS", "WAT", "HEAVY_RAIL", 1800)],
    )

    result = scanner.scan_route(route_id)  # must not raise
    gtfs_rows = get_scan_results(db, route_id, source="gtfs")
    maps_rows = get_scan_results(db, route_id, source="maps")
    assert all(r["status"] == "UNKNOWN" for r in gtfs_rows)
    assert any(r["status"] == "NORMAL" for r in maps_rows)
    assert result["route_id"] == route_id


# 9. Partial GTFS baseline (one leg NULL) -> that leg UNKNOWN, other legs scan normally
def test_scan_route_partial_gtfs_baseline_writes_unknown_per_leg(db, monkeypatch):
    route_id = insert_route(db, scan_source="gtfs")
    insert_gtfs_baseline(db, route_id, only_outbound_leg1=True, trip_count_by_weekday={str(TODAY_WD): 5})
    _use_single_gtfs_scan_day(db, route_id)

    _stub_gtfs_journeys(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[_gtfs_journey()]))
    _stub_gtfs_direct_trips(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[{}] * 5))

    scanner.scan_route(route_id)
    rows = {r["direction"]: r for r in get_scan_results(db, route_id, source="gtfs")}
    assert rows["outbound"]["status"] == "NORMAL"
    assert rows["return"]["status"] == "UNKNOWN"
    assert "no GTFS baseline captured for this leg" in json.loads(rows["return"]["disruption_reasons"])


# 10. Maps baseline missing still raises (asymmetry preserved)
def test_scan_route_maps_baseline_missing_still_raises(db):
    route_id = insert_route(db, scan_source="maps")
    with pytest.raises(ValueError):
        scanner.scan_route(route_id)

    route_id2 = insert_route(db, scan_source="both")
    with pytest.raises(ValueError):
        scanner.scan_route(route_id2)


# 11. GTFS overrides control the GTFS-side date range independently
def test_gtfs_dates_use_gtfs_overrides_when_set(db):
    route_id = insert_route(
        db, scan_days="0,1,2,3,4,5,6", lookahead_weeks=4,
        gtfs_scan_days=str(TODAY_WD), gtfs_lookahead_weeks=1,
    )
    row = db.get_db().execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
    n, _ = scanner._scan_gtfs(row, None)
    # today+7 days window with only today's weekday selected -> 1 or 2 matches (day 0 and/or day 7)
    assert n in (1, 2)


# 12. Null GTFS override columns fall back to Maps values
def test_gtfs_dates_fall_back_to_maps_when_null(db):
    route_id = insert_route(db, scan_days=str(TODAY_WD), lookahead_weeks=1)
    row = db.get_db().execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
    n, _ = scanner._scan_gtfs(row, None)
    assert n in (1, 2)


# 13. Empty-string gtfs_scan_days falls back instead of crashing
def test_gtfs_scan_days_empty_string_falls_back_not_crashes(db):
    route_id = insert_route(db, scan_days=str(TODAY_WD), lookahead_weeks=1)
    conn = db.get_db()
    try:
        conn.execute("UPDATE routes SET gtfs_scan_days='' WHERE id=?", (route_id,))
        conn.commit()
    finally:
        conn.close()
    row = db.get_db().execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
    n, _ = scanner._scan_gtfs(row, None)  # must not raise
    assert n in (1, 2)


# 14. GET .../baseline?source= param
def test_get_baseline_endpoint_source_param(db, monkeypatch):
    from fastapi.testclient import TestClient
    from routers import admin as admin_router

    route_id = insert_route(db, scan_source="both")
    insert_maps_baseline(db, route_id, arr_stop="WAT-MAPS")
    insert_gtfs_baseline(db, route_id, dest_crs="WAT-GTFS")

    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(admin_router.router)
    client = TestClient(app)

    r = client.get(f"/api/routes/{route_id}/baseline")
    assert r.status_code == 200
    assert r.json()["outbound_leg1"]["arr_stop"] == "WAT-MAPS"

    r2 = client.get(f"/api/routes/{route_id}/baseline?source=gtfs")
    assert r2.status_code == 200
    assert r2.json()["outbound_leg1"]["arr_stop"] == "WAT-GTFS"


# 15. confirm_gtfs_baseline writes source='gtfs', preserves Maps baseline
def test_confirm_gtfs_baseline_writes_source_row_and_preserves_maps(db, monkeypatch):
    route_id = insert_route(db, scan_source="both", scan_days=str(TODAY_WD), gtfs_scan_days=str(TODAY_WD))
    insert_maps_baseline(db, route_id, arr_stop="WAT-MAPS")

    _stub_gtfs_direct_trips(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[{}] * 4))

    scanner.confirm_gtfs_baseline(route_id, "2026-08-01", {
        "outbound_leg1": {"duration_s": 1700, "departure_time": "12:00", "dep_stop": "BNS", "arr_stop": "WAT-GTFS", "intermediate_stops": []},
        "outbound_leg2": None,
        "return_leg1": {"duration_s": 1700, "departure_time": "12:00", "dep_stop": "WAT-GTFS", "arr_stop": "BNS", "intermediate_stops": []},
        "return_leg2": None,
    })

    conn = db.get_db()
    try:
        maps_row = conn.execute("SELECT * FROM baselines WHERE route_id=? AND source='maps'", (route_id,)).fetchone()
        gtfs_row = conn.execute("SELECT * FROM baselines WHERE route_id=? AND source='gtfs'", (route_id,)).fetchone()
    finally:
        conn.close()
    assert maps_row["outbound_leg1_arr_stop"] == "WAT-MAPS"
    assert gtfs_row["outbound_leg1_arr_stop"] == "WAT-GTFS"


# 16. confirm_gtfs_baseline stores per-weekday trip counts across the effective scan weekdays
def test_confirm_gtfs_baseline_per_weekday_counts(db, monkeypatch):
    route_id = insert_route(db, gtfs_scan_days="5,6")

    calls = []

    def fake_direct_trips(o, d, date, time_, window_minutes=60):
        wd = datetime.date.fromisoformat(date).weekday()
        calls.append(wd)
        return gtfs_api.GtfsResult(ok=True, journeys=[{}] * (10 if wd == 5 else 6))

    monkeypatch.setattr(gtfs_api, "fetch_gtfs_direct_trips", fake_direct_trips)

    scanner.confirm_gtfs_baseline(route_id, "2026-08-01", {
        "outbound_leg1": {"duration_s": 1700, "departure_time": "12:00", "dep_stop": "BNS", "arr_stop": "WAT", "intermediate_stops": []},
        "outbound_leg2": None,
        "return_leg1": {"duration_s": 1700, "departure_time": "12:00", "dep_stop": "WAT", "arr_stop": "BNS", "intermediate_stops": []},
        "return_leg2": None,
    })

    assert set(calls) >= {5, 6}
    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM baselines WHERE route_id=? AND source='gtfs'", (route_id,)).fetchone()
    finally:
        conn.close()
    steps = json.loads(row["outbound_leg1_steps"])
    assert steps["trip_count_by_weekday"]["5"] == 10
    assert steps["trip_count_by_weekday"]["6"] == 6


# 17. Interchange journeys excluded from direct match
def test_interchange_journeys_excluded_from_direct_match():
    journeys = [_gtfs_journey(kind="interchange"), _gtfs_journey(kind="direct")]
    direct = scanner._direct_journeys(journeys)
    assert len(direct) == 1
    assert direct[0]["kind"] == "direct"


# 18. list_routes reports per-source baseline flags
def test_list_routes_reports_per_source_baseline_flags(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import admin as admin_router

    route_id = insert_route(db)
    insert_gtfs_baseline(db, route_id)

    app = FastAPI()
    app.include_router(admin_router.router)
    client = TestClient(app)
    r = client.get("/api/routes")
    assert r.status_code == 200
    row = next(x for x in r.json() if x["id"] == route_id)
    assert row["has_gtfs_baseline"] is True
    assert row["has_maps_baseline"] is False
    assert row["has_baseline"] is False


# 19. Route create/update accept GTFS fields, with validation
def test_route_create_update_accept_gtfs_fields(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import admin as admin_router
    from stations import load_station_list

    load_station_list()
    app = FastAPI()
    app.include_router(admin_router.router)
    client = TestClient(app)

    r = client.post("/api/routes", json={
        "origin_crs": "bns", "destination_crs": "wat", "scan_days": [5, 6],
        "scan_source": "both", "gtfs_lookahead_weeks": 8, "gtfs_scan_days": [0, 1],
    })
    assert r.status_code == 201
    route_id = r.json()["id"]

    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
    finally:
        conn.close()
    assert row["scan_source"] == "both"
    assert row["gtfs_lookahead_weeks"] == 8
    assert row["gtfs_scan_days"] == "0,1"

    r2 = client.post("/api/routes", json={
        "origin_crs": "BNS", "destination_crs": "WAT", "scan_days": [5],
        "gtfs_scan_days": [],
    })
    assert r2.status_code == 422

    r3 = client.post("/api/routes", json={
        "origin_crs": "BNS", "destination_crs": "WAT", "scan_days": [5],
        "gtfs_lookahead_weeks": 0,
    })
    assert r3.status_code == 422

    r4 = client.patch(f"/api/routes/{route_id}", json={"scan_source": "gtfs"})
    assert r4.status_code == 200
    conn = db.get_db()
    try:
        row = conn.execute("SELECT scan_source FROM routes WHERE id=?", (route_id,)).fetchone()
    finally:
        conn.close()
    assert row["scan_source"] == "gtfs"


# 20. reports.py holding fix: 'both' route doesn't duplicate legs / double-count
def test_reports_holding_fix_filters_maps_source(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import reports as reports_router

    route_id = insert_route(db, scan_source="both")
    insert_maps_baseline(db, route_id)
    _use_single_scan_day(db, route_id)
    _use_single_gtfs_scan_day(db, route_id)

    monkeypatch.setattr(scanner, "get_coords", lambda crs: (0.0, 0.0))
    monkeypatch.setattr(
        scanner, "compute_all_routes",
        lambda *a, **k: [_fake_maps_route("BNS", "WAT", "HEAVY_RAIL", 1800)],
    )
    _stub_gtfs_journeys(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[]))
    _stub_gtfs_direct_trips(monkeypatch, gtfs_api.GtfsResult(ok=True, journeys=[]))
    scanner.scan_route(route_id)  # writes both maps + gtfs rows

    app = FastAPI()
    app.include_router(reports_router.router)
    client = TestClient(app)
    r = client.get("/api/reports")
    assert r.status_code == 200
    route_data = next(x for x in r.json() if x["id"] == route_id)
    for day in route_data["per_day"].values():
        assert len(day["legs"]) == len(set(leg["key"] for leg in day["legs"]))

    # Same holding fix must apply to the per-route detail endpoint too, not just the list one.
    r2 = client.get(f"/api/reports/{route_id}")
    assert r2.status_code == 200
    results = r2.json()["results"]
    keys = [(res["target_date"], res["direction"], res["leg"]) for res in results]
    assert len(keys) == len(set(keys))
