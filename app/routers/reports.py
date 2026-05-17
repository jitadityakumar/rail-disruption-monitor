import json
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import get_db
from stations import get_station_name

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _route_leg_labels(row) -> list[dict]:
    o = get_station_name(row["origin_crs"]) or row["origin_crs"]
    d = get_station_name(row["destination_crs"]) or row["destination_crs"]
    c = (get_station_name(row["change_crs"]) or row["change_crs"]) if row["change_crs"] else None
    if c:
        return [
            {"key": "outbound_1", "label": f"{o} → {c}"},
            {"key": "outbound_2", "label": f"{c} → {d}"},
            {"key": "return_1",   "label": f"{d} → {c}"},
            {"key": "return_2",   "label": f"{c} → {o}"},
        ]
    return [
        {"key": "outbound_1", "label": f"{o} → {d}"},
        {"key": "return_1",   "label": f"{d} → {o}"},
    ]


@router.get("/reports", response_class=HTMLResponse)
def get_reports_page(request: Request):
    return templates.TemplateResponse("reports.html", {"request": request})


@router.get("/api/reports")
def get_reports():
    db = get_db()
    routes = db.execute("SELECT * FROM routes ORDER BY created_at").fetchall()
    result = []
    for route in routes:
        route_dict = dict(route)
        route_dict["scan_days"] = [int(x) for x in route_dict["scan_days"].split(",")]
        route_dict["kiosk_visible"] = bool(route_dict["kiosk_visible"])
        route_dict["has_change"] = bool(route["change_crs"])
        route_dict["leg_labels"] = _route_leg_labels(route)

        scan_rows = db.execute(
            """SELECT target_date, direction, leg, status, duration_s, disruption_reasons, scanned_at
               FROM scan_results WHERE route_id = ?
               AND target_date >= date('now')
               AND target_date <= date('now', ? || ' days')
               ORDER BY target_date, direction, leg""",
            (route["id"], route["lookahead_weeks"] * 7),
        ).fetchall()

        by_date = defaultdict(dict)
        for r in scan_rows:
            key = f"{r['direction']}_{r['leg']}"
            by_date[r["target_date"]][key] = {
                "status": r["status"],
                "duration_s": r["duration_s"],
                "disruption_reasons": json.loads(r["disruption_reasons"] or "[]"),
                "scanned_at": r["scanned_at"],
            }

        route_dict["results_by_date"] = {
            date: slots for date, slots in sorted(by_date.items())
        }
        result.append(route_dict)
    db.close()
    return result


@router.get("/api/reports/{route_id}")
def get_route_report(route_id: int):
    db = get_db()
    route = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if not route:
        db.close()
        raise HTTPException(status_code=404, detail="Route not found")

    scan_rows = db.execute(
        "SELECT * FROM scan_results WHERE route_id = ? ORDER BY target_date, direction, leg",
        (route_id,),
    ).fetchall()
    db.close()

    results = []
    for r in scan_rows:
        results.append({
            "target_date": r["target_date"],
            "direction": r["direction"],
            "leg": r["leg"],
            "key": f"{r['direction']}_{r['leg']}",
            "status": r["status"],
            "duration_s": r["duration_s"],
            "steps": json.loads(r["steps"] or "[]"),
            "disruption_reasons": json.loads(r["disruption_reasons"] or "[]"),
            "scanned_at": r["scanned_at"],
        })

    return {
        "route": {
            "id": route["id"],
            "name": route["name"],
            "origin_crs": route["origin_crs"],
            "change_crs": route["change_crs"],
            "destination_crs": route["destination_crs"],
            "leg_labels": _route_leg_labels(route),
        },
        "results": results,
    }
