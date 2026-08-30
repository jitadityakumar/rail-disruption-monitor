import json

from conftest import insert_both_baselines, insert_route
from routers.reports import _build_route_data, _derive_issues


def _insert_scan_result(db, route_id, target_date, direction, status, duration_s=1800,
                         alternate_steps=None, disruption_reasons=None):
    conn = db.get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO scan_results
               (route_id, target_date, direction, status, duration_s, matched_steps,
                alternate_steps, disruption_reasons, calls_made, window_fully_walked)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 1, 1)""",
            (route_id, target_date, direction, status, duration_s,
             json.dumps(alternate_steps) if alternate_steps is not None else None,
             json.dumps(disruption_reasons or [])),
        )
        conn.commit()
    finally:
        conn.close()


def test_29_reports_endpoint_one_row_per_date_direction(db):
    route_id = insert_route(db)
    insert_both_baselines(db, route_id)
    _insert_scan_result(db, route_id, "2026-09-01", "outbound", "NORMAL")
    _insert_scan_result(db, route_id, "2026-09-01", "return", "NORMAL")

    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as client:
        resp = client.get(f"/api/reports/{route_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 2
    for r in body["results"]:
        assert "source" not in r
        assert "rollup" not in r


def test_30_day_status_derivation(db):
    route_id = insert_route(db)
    insert_both_baselines(db, route_id)
    _insert_scan_result(db, route_id, "2026-09-01", "outbound", "NORMAL")
    _insert_scan_result(db, route_id, "2026-09-01", "return", "NORMAL")
    _insert_scan_result(db, route_id, "2026-09-02", "outbound", "DISRUPTED")
    _insert_scan_result(db, route_id, "2026-09-02", "return", "NORMAL")

    conn = db.get_db()
    try:
        routes = _build_route_data(conn)
    finally:
        conn.close()

    per_day = routes[0]["per_day"]
    assert per_day["2026-09-01"]["status"] == "clear"
    assert per_day["2026-09-02"]["status"] == "disrupted"


def test_31_derive_issues_bus_leg_mode_structural():
    baseline = {
        "interchange_stops": json.dumps([]),
        "leg_modes": json.dumps(["national-rail"]),
        "duration_s": 1800,
    }
    alternate_steps = [{"mode": "bus", "summary": "K3 bus replacement"}]
    issues = _derive_issues(baseline, 2000, alternate_steps, [])
    assert any(i["type"] == "bus" and i["title"] == "Bus replacement" for i in issues)
