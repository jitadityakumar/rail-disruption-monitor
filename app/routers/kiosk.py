import datetime
import json
from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import get_db
from stations import get_station_name

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _leg_label(row, direction: str, leg: int) -> str:
    o = get_station_name(row["origin_crs"]) or row["origin_crs"]
    d = get_station_name(row["destination_crs"]) or row["destination_crs"]
    c = (get_station_name(row["change_crs"]) or row["change_crs"]) if row["change_crs"] else None

    labels = {
        ("outbound", 1): f"{o} → {c or d}",
        ("outbound", 2): f"{c} → {d}" if c else None,
        ("return",   1): f"{d} → {c or o}",
        ("return",   2): f"{c} → {o}" if c else None,
    }
    return labels.get((direction, leg), f"{direction} leg {leg}")


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
                "label": _leg_label(route, r["direction"], r["leg"]),
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
