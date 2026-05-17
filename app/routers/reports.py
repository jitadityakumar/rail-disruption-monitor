import json
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import get_db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


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
        route_dict["crs_sequence"] = json.loads(route_dict["crs_sequence"])
        route_dict["scan_days"] = [int(x) for x in route_dict["scan_days"].split(",")]
        route_dict["kiosk_visible"] = bool(route_dict["kiosk_visible"])

        scan_rows = db.execute(
            """SELECT target_date, time_slot, status, duration_s, disruption_reasons, scanned_at
               FROM scan_results WHERE route_id = ?
               ORDER BY target_date, time_slot""",
            (route["id"],),
        ).fetchall()

        by_date = defaultdict(dict)
        for r in scan_rows:
            by_date[r["target_date"]][r["time_slot"]] = {
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
        """SELECT * FROM scan_results WHERE route_id = ?
           ORDER BY target_date, time_slot""",
        (route_id,),
    ).fetchall()
    db.close()

    results = []
    for r in scan_rows:
        results.append({
            "target_date": r["target_date"],
            "time_slot": r["time_slot"],
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
            "crs_sequence": json.loads(route["crs_sequence"]),
        },
        "results": results,
    }
