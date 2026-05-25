import json
from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from database import get_db
from shared_templates import templates
from stations import get_station_name, route_display_name, route_leg_labels

router = APIRouter()


def _derive_issues(
    reasons: list[str],
    steps: list[dict],
    baseline_duration_s: int | None,
    scan_duration_s: int | None,
) -> list[dict]:
    issues: list[dict] = []
    seen: set[str] = set()

    def add(issue_type: str, title: str, pill: str | None = None) -> None:
        if issue_type not in seen:
            seen.add(issue_type)
            issues.append({"type": issue_type, "title": title, "pill": pill})

    if any(s.get("vehicle_type") == "BUS" for s in steps):
        add("bus", "Bus replacement")

    for reason in reasons:
        r = reason.lower()
        if "bus" in r or "replacement" in r:
            add("bus", "Bus replacement")
        elif "longer" in r:
            pill = None
            if baseline_duration_s and scan_duration_s:
                delta_min = round((scan_duration_s - baseline_duration_s) / 60)
                if delta_min >= 1:
                    pill = f"+{delta_min} min"
            add("time", "Journey time longer", pill)
        elif "route" in r or "stop sequence" in r or "no direct" in r:
            add("route", "No direct route found")
        else:
            issues.append({"type": "other", "title": reason, "pill": None})

    return issues


def _build_route_data(db, kiosk_only: bool = False) -> list:
    where = "WHERE kiosk_visible = 1" if kiosk_only else ""
    routes = db.execute(f"SELECT * FROM routes {where} ORDER BY created_at").fetchall()
    result = []
    today = date.today()

    for route in routes:
        route_dict = dict(route)
        route_dict["scan_days"] = [int(x) for x in route_dict["scan_days"].split(",")]
        route_dict["kiosk_visible"] = bool(route_dict["kiosk_visible"])
        route_dict["has_change"] = bool(route["change_crs"])
        route_dict["leg_labels"] = route_leg_labels(route)
        route_dict["display_name"] = route_display_name(
            route["origin_crs"], route["destination_crs"], route["change_crs"]
        )
        route_dict["scan_weekdays"] = route_dict["scan_days"]
        route_dict["origin_name"] = get_station_name(route["origin_crs"]) or route["origin_crs"]
        route_dict["destination_name"] = get_station_name(route["destination_crs"]) or route["destination_crs"]
        route_dict["change_name"] = (
            (get_station_name(route["change_crs"]) or route["change_crs"])
            if route["change_crs"]
            else None
        )

        baseline = db.execute(
            """SELECT outbound_leg1_duration_s, outbound_leg2_duration_s,
                      return_leg1_duration_s, return_leg2_duration_s
               FROM baselines WHERE route_id = ?""",
            (route["id"],),
        ).fetchone()
        baseline_dict = dict(baseline) if baseline else {}

        scan_rows = db.execute(
            """SELECT target_date, direction, leg, status, duration_s, steps, disruption_reasons, scanned_at
               FROM scan_results WHERE route_id = ?
               AND target_date >= date('now')
               AND target_date <= date('now', ? || ' days')
               ORDER BY target_date, direction, leg""",
            (route["id"], route["lookahead_weeks"] * 7),
        ).fetchall()

        leg_label_map = {lbl["key"]: lbl["label"] for lbl in route_dict["leg_labels"]}
        per_day: dict[str, dict] = {}
        by_date: dict[str, dict] = defaultdict(dict)

        for r in scan_rows:
            key = f"{r['direction']}_{r['leg']}"
            reasons = json.loads(r["disruption_reasons"] or "[]")
            steps = json.loads(r["steps"] or "[]")

            bl_key = f"{r['direction']}_leg{r['leg']}_duration_s"
            bl_dur = baseline_dict.get(bl_key)
            issues = _derive_issues(reasons, steps, bl_dur, r["duration_s"])

            by_date[r["target_date"]][key] = {
                "status": r["status"],
                "duration_s": r["duration_s"],
                "disruption_reasons": reasons,
                "scanned_at": r["scanned_at"],
            }

            if r["target_date"] not in per_day:
                per_day[r["target_date"]] = {"status": None, "legs": []}
            per_day[r["target_date"]]["legs"].append({
                "key": key,
                "label": leg_label_map.get(key, key),
                "status": r["status"],
                "duration_s": r["duration_s"],
                "issues": issues,
                "reasons": reasons,
            })

        for day in per_day.values():
            statuses = {leg["status"] for leg in day["legs"]}
            if "DISRUPTED" in statuses:
                day["status"] = "disrupted"
            elif statuses == {"NORMAL"}:
                day["status"] = "clear"
            else:
                day["status"] = "unknown"

        route_dict["disrupted_day_count"] = sum(
            1 for d in per_day.values() if d["status"] == "disrupted"
        )

        scan_days_set = set(route_dict["scan_days"])
        lookahead_end = today + timedelta(days=route["lookahead_weeks"] * 7)
        first_clear = None
        cur = today + timedelta(days=1)
        while cur <= lookahead_end:
            if cur.weekday() in scan_days_set:
                ds = cur.isoformat()
                if ds in per_day and per_day[ds]["status"] == "clear":
                    first_clear = ds
                    break
            cur += timedelta(days=1)
        route_dict["first_clear_date"] = first_clear

        route_dict["per_day"] = per_day
        route_dict["results_by_date"] = {d: slots for d, slots in sorted(by_date.items())}
        result.append(route_dict)

    return result


@router.get("/reports", response_class=HTMLResponse)
def get_reports_page(request: Request):
    return templates.TemplateResponse("reports.html", {"request": request})


@router.get("/api/reports")
def get_reports():
    db = get_db()
    try:
        return _build_route_data(db)
    finally:
        db.close()


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
            "display_name": route_display_name(
                route["origin_crs"], route["destination_crs"], route["change_crs"]
            ),
            "origin_crs": route["origin_crs"],
            "change_crs": route["change_crs"],
            "destination_crs": route["destination_crs"],
            "leg_labels": route_leg_labels(route),
        },
        "results": results,
    }
