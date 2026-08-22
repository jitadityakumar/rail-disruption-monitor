import datetime
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import insert_gtfs_baseline, insert_maps_baseline, insert_route
from routers import reports as reports_router

TODAY = datetime.date.today()
TODAY_WD = TODAY.weekday()
FUTURE_DATE = (TODAY + datetime.timedelta(days=3)).isoformat()
FAR_FUTURE_DATE = (TODAY + datetime.timedelta(weeks=6)).isoformat()


def _client():
    app = FastAPI()
    app.include_router(reports_router.router)
    return TestClient(app)


def _save_result(db, route_id, target_date, direction, leg, status, source, duration_s=1800, reasons=None):
    conn = db.get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO scan_results
               (route_id, target_date, direction, leg, status, duration_s, steps, disruption_reasons, source)
               VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?)""",
            (route_id, target_date, direction, leg, status, duration_s, json.dumps(reasons or []), source),
        )
        conn.commit()
    finally:
        conn.close()


# 1. Maps/GTFS disagreement surfaces end-to-end via /api/reports
def test_reports_api_surfaces_disagreement(db):
    route_id = insert_route(
        db, scan_source="both", scan_days=str(TODAY_WD), gtfs_scan_days=str(TODAY_WD),
        lookahead_weeks=1,
    )
    insert_maps_baseline(db, route_id)
    insert_gtfs_baseline(db, route_id)

    _save_result(db, route_id, FUTURE_DATE, "outbound", 1, "NORMAL", "maps")
    _save_result(db, route_id, FUTURE_DATE, "outbound", 1, "DISRUPTED", "gtfs", reasons=["no scheduled service found (GTFS)"])
    _save_result(db, route_id, FUTURE_DATE, "return", 1, "NORMAL", "maps")
    _save_result(db, route_id, FUTURE_DATE, "return", 1, "NORMAL", "gtfs")

    client = _client()
    resp = client.get("/api/reports")
    assert resp.status_code == 200
    routes = resp.json()
    route = next(r for r in routes if r["id"] == route_id)

    day = route["per_day"][FUTURE_DATE]
    assert day["status"] == "disagree"

    leg = next(leg for leg in day["legs"] if leg["key"] == "outbound_1")
    assert leg["rollup"] == "disagree_gtfs_flags"
    assert leg["maps_status"] == "NORMAL"
    assert leg["gtfs_status"] == "DISRUPTED"


# 2. Row beyond lookahead_weeks but within gtfs_lookahead_weeks is present
def test_reports_api_includes_extended_gtfs_lookahead_row(db):
    route_id = insert_route(
        db, scan_source="both", scan_days=str(TODAY_WD), gtfs_scan_days=str(TODAY_WD),
        lookahead_weeks=1, gtfs_lookahead_weeks=8,
    )
    insert_maps_baseline(db, route_id)
    insert_gtfs_baseline(db, route_id)

    _save_result(db, route_id, FAR_FUTURE_DATE, "outbound", 1, "NORMAL", "gtfs")
    _save_result(db, route_id, FAR_FUTURE_DATE, "return", 1, "NORMAL", "gtfs")

    client = _client()
    resp = client.get("/api/reports")
    routes = resp.json()
    route = next(r for r in routes if r["id"] == route_id)

    assert FAR_FUTURE_DATE in route["per_day"]
    assert route["per_day"][FAR_FUTURE_DATE]["status"] == "clear_gtfs_only"


# 3. Baseline duration comparison uses the Maps baseline, not an arbitrary row
def test_derive_issues_uses_maps_baseline_not_gtfs(db):
    route_id = insert_route(
        db, scan_source="both", scan_days=str(TODAY_WD), gtfs_scan_days=str(TODAY_WD),
        lookahead_weeks=1,
    )
    insert_maps_baseline(db, route_id, duration_s=1800)
    insert_gtfs_baseline(db, route_id, duration_s=99999)  # would blow up the delta if picked up

    # Maps scan comes back 60s over the Maps baseline (1800s) — the scanner would have flagged
    # this as DISRUPTED with a "longer" reason. If the GTFS baseline's wildly different duration
    # (99999s) were picked up instead of the Maps one, the pill's minute delta would be tens of
    # thousands of minutes instead of ~1.
    _save_result(
        db, route_id, FUTURE_DATE, "outbound", 1, "DISRUPTED", "maps", duration_s=1860,
        reasons=["Journey 3% longer than baseline (31m vs 30m)"],
    )
    _save_result(db, route_id, FUTURE_DATE, "return", 1, "NORMAL", "maps", duration_s=1800)

    client = _client()
    resp = client.get("/api/reports")
    routes = resp.json()
    route = next(r for r in routes if r["id"] == route_id)

    day = route["per_day"][FUTURE_DATE]
    leg = next(leg for leg in day["legs"] if leg["key"] == "outbound_1")
    time_issue = next(issue for issue in leg["issues"] if issue["type"] == "time")
    assert time_issue["pill"] == "+1 min"
