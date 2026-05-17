import datetime
import json
from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import get_db
from stations import leg_label, route_display_name

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/kiosk", response_class=HTMLResponse)
def get_kiosk_page(request: Request):
    return templates.TemplateResponse("kiosk.html", {"request": request})


@router.get("/api/kiosk")
def get_kiosk_data():
    db = get_db()
    routes = db.execute(
        "SELECT * FROM routes WHERE kiosk_visible = 1 ORDER BY created_at"
    ).fetchall()

    today = datetime.date.today().isoformat()
    result = []

    for route in routes:
        scan_rows = db.execute(
            """SELECT target_date, direction, leg, disruption_reasons, scanned_at
               FROM scan_results
               WHERE route_id = ? AND target_date >= ? AND status = 'DISRUPTED'
               AND target_date <= date('now', ? || ' days')
               ORDER BY target_date, direction, leg""",
            (route["id"], today, route["lookahead_weeks"] * 7),
        ).fetchall()

        disruptions_by_date = defaultdict(list)
        for r in scan_rows:
            disruptions_by_date[r["target_date"]].append({
                "label": leg_label(route, r["direction"], r["leg"]),
                "disruption_reasons": json.loads(r["disruption_reasons"] or "[]"),
                "scanned_at": r["scanned_at"],
            })

        crs_parts = [route["origin_crs"]]
        if route["change_crs"]:
            crs_parts.append(route["change_crs"])
        crs_parts.append(route["destination_crs"])

        result.append({
            "id": route["id"],
            "name": route["name"],
            "display_name": route_display_name(route["origin_crs"], route["destination_crs"], route["change_crs"]),
            "crs_display": " → ".join(crs_parts),
            "last_scanned_at": route["last_scanned_at"],
            "disruptions": [
                {"date": date, "slots": slots}
                for date, slots in sorted(disruptions_by_date.items())
            ],
        })

    db.close()
    result.sort(key=lambda r: r["disruptions"][0]["date"] if r["disruptions"] else "9999-99-99")
    return result
