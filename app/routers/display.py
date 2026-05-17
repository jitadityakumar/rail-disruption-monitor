import datetime
import json
from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import get_db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/display", response_class=HTMLResponse)
def get_display_page(request: Request):
    return templates.TemplateResponse("display.html", {"request": request})


@router.get("/api/display")
def get_display_data():
    db = get_db()
    routes = db.execute(
        "SELECT * FROM routes WHERE kiosk_visible = 1 ORDER BY created_at"
    ).fetchall()

    today = datetime.date.today().isoformat()
    result = []

    for route in routes:
        scan_rows = db.execute(
            """SELECT target_date, time_slot, status, disruption_reasons, scanned_at
               FROM scan_results
               WHERE route_id = ? AND target_date >= ? AND status = 'DISRUPTED'
               ORDER BY target_date, time_slot""",
            (route["id"], today),
        ).fetchall()

        disruptions_by_date = defaultdict(list)
        for r in scan_rows:
            disruptions_by_date[r["target_date"]].append({
                "time_slot": r["time_slot"],
                "disruption_reasons": json.loads(r["disruption_reasons"] or "[]"),
                "scanned_at": r["scanned_at"],
            })

        result.append({
            "id": route["id"],
            "name": route["name"],
            "crs_sequence": json.loads(route["crs_sequence"]),
            "last_scanned_at": route["last_scanned_at"],
            "disruptions": [
                {"date": date, "slots": slots}
                for date, slots in sorted(disruptions_by_date.items())
            ],
        })

    db.close()
    result.sort(key=lambda r: r["disruptions"][0]["date"] if r["disruptions"] else "9999-99-99")
    return result
