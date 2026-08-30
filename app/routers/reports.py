import json
from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import scanner
from database import get_db
from display import route_direction_labels, route_display_name
from shared_templates import templates

router = APIRouter()

_BUS_MODES = {"bus", "replacement-bus"}


_CHAIN_ARROWS = {"national-rail": "⇄", "bus": "⦵", "replacement-bus": "⦵"}
_CHAIN_ARROW_DEFAULT = "⟶"


def route_chain_label(steps: list[dict] | None) -> str | None:
    """"Barnes Rail Station <arrow> Clapham Junction Rail Station <arrow> ..." -- the full chain
    of stations the alternate itinerary actually passes through (walking legs excluded, since
    they're inconsistently itemized by TfL -- see tfl_client comments), with the arrow between
    each pair naming that leg's mode: U+21C4 for national rail, U+29B5 for any bus mode, U+27F6
    for anything else. None if there's no usable itinerary to show."""
    transit_steps = [s for s in (steps or []) if s.get("mode") != "walking"]
    if not transit_steps:
        return None
    parts = [transit_steps[0]["dep_name"]]
    for leg in transit_steps:
        parts.append(_CHAIN_ARROWS.get(leg.get("mode"), _CHAIN_ARROW_DEFAULT))
        parts.append(leg["arr_name"])
    return " ".join(parts)


def _derive_issues(baseline, duration_s, alternate_steps) -> list[dict]:
    issues: list[dict] = []
    seen: set[str] = set()

    def add(issue_type: str, title: str, pill: str | None = None) -> None:
        if issue_type not in seen:
            seen.add(issue_type)
            issues.append({"type": issue_type, "title": title, "pill": pill})

    # TfL's JourneyResults only ever returns complete, valid itineraries -- a structural
    # difference from baseline (extra interchanges, different stations) tells us *something*
    # changed but never *where* the actual disruption is, so we don't try to guess or surface a
    # "No direct route found" / raw disruptions[] reason here. The alternate itinerary itself
    # (see alternate_steps / route_chain_label) is shown instead, as the concrete fact TfL gave us.
    transit_steps = [s for s in (alternate_steps or []) if s.get("mode") != "walking"]
    leg_modes = [s.get("mode") for s in transit_steps]

    if any(m in _BUS_MODES for m in leg_modes):
        add("bus", "Bus replacement")

    if baseline is not None:
        baseline_duration_s = baseline["duration_s"]
        if baseline_duration_s and duration_s and duration_s > baseline_duration_s:
            delta_min = round((duration_s - baseline_duration_s) / 60)
            pill = f"+{delta_min} min" if delta_min >= 1 else None
            add("time", "Journey time longer", pill)

    return issues


def _build_route_data(db, kiosk_only: bool = False) -> list:
    where = "WHERE kiosk_visible = 1" if kiosk_only else ""
    routes = db.execute(f"SELECT * FROM routes {where} ORDER BY created_at").fetchall()
    result = []
    today = date.today()

    window_end = scanner.scan_window_end_date(today)

    for route in routes:
        route_dict = dict(route)
        route_dict["kiosk_visible"] = bool(route_dict["kiosk_visible"])
        route_dict["display_name"] = route_display_name(route)
        route_dict["direction_labels"] = route_direction_labels(route)
        route_dict["scan_window_end"] = window_end.isoformat()

        baselines = {
            b["direction"]: b
            for b in db.execute(
                "SELECT * FROM baselines WHERE route_id = ?", (route["id"],)
            ).fetchall()
        }

        scan_rows = db.execute(
            """SELECT target_date, direction, status, duration_s, matched_steps, alternate_steps,
                      disruption_reasons, scanned_at
               FROM scan_results WHERE route_id = ?
               AND target_date >= date('now')
               AND target_date <= ?
               ORDER BY target_date, direction""",
            (route["id"], window_end.isoformat()),
        ).fetchall()

        direction_label_map = {lbl["key"]: lbl["label"] for lbl in route_dict["direction_labels"]}

        grouped: dict[str, dict[str, object]] = defaultdict(dict)
        for r in scan_rows:
            grouped[r["target_date"]][r["direction"]] = r

        per_day: dict[str, dict] = {}
        by_date: dict[str, dict] = defaultdict(dict)

        for target_date, legs_by_direction in grouped.items():
            per_day[target_date] = {"status": None, "legs": []}
            statuses = []
            for direction, row in legs_by_direction.items():
                status = row["status"]
                statuses.append(status)
                duration_s = row["duration_s"]
                alternate_steps = json.loads(row["alternate_steps"] or "null")
                baseline = baselines.get(direction)
                issues = _derive_issues(baseline, duration_s, alternate_steps)

                by_date[target_date][direction] = {
                    "status": status,
                    "duration_s": duration_s,
                    "scanned_at": row["scanned_at"],
                }

                per_day[target_date]["legs"].append({
                    "key": direction,
                    "label": direction_label_map.get(direction, direction),
                    "status": status,
                    "duration_s": duration_s,
                    "issues": issues,
                    "route_chain": route_chain_label(alternate_steps),
                })

            if any(s == "DISRUPTED" for s in statuses):
                per_day[target_date]["status"] = "disrupted"
            elif all(s == "NORMAL" for s in statuses) and statuses:
                per_day[target_date]["status"] = "clear"
            else:
                per_day[target_date]["status"] = "unknown"

        route_dict["disrupted_day_count"] = sum(
            1 for d in per_day.values() if d["status"] == "disrupted"
        )

        first_clear = None
        cur = today + timedelta(days=1)
        while cur <= window_end:
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
    return templates.TemplateResponse(request, "reports.html")


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
    try:
        route = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        if not route:
            raise HTTPException(status_code=404, detail="Route not found")

        scan_rows = db.execute(
            "SELECT * FROM scan_results WHERE route_id = ? ORDER BY target_date, direction",
            (route_id,),
        ).fetchall()
    finally:
        db.close()

    results = []
    for r in scan_rows:
        results.append({
            "target_date": r["target_date"],
            "direction": r["direction"],
            "key": f"{r['direction']}",
            "status": r["status"],
            "duration_s": r["duration_s"],
            "matched_steps": json.loads(r["matched_steps"] or "null"),
            "alternate_steps": json.loads(r["alternate_steps"] or "null"),
            "disruption_reasons": json.loads(r["disruption_reasons"] or "[]"),
            "scanned_at": r["scanned_at"],
        })

    return {
        "route": {
            "id": route["id"],
            "name": route["name"],
            "display_name": route_display_name(route),
            "origin_stop_id": route["origin_stop_id"],
            "destination_stop_id": route["destination_stop_id"],
            "direction_labels": route_direction_labels(route),
        },
        "results": results,
    }
