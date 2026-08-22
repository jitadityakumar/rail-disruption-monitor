import json
from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from database import get_db
from rollup import day_rollup_status, leg_rollup_status
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


def _leg_status(row) -> str | None:
    return row["status"] if row is not None else None


def _display_status(rollup: str) -> str:
    """Legacy single-value NORMAL/DISRUPTED/UNKNOWN status, derived from the rollup, kept
    for backward compatibility with UI code that filters on leg["status"] (e.g. the
    disruption list / day-detail modal). Exact rollup-aware rendering is issue #18's job."""
    if rollup in ("disrupted", "agree_disrupted", "disagree_gtfs_flags", "disagree_maps_flags"):
        return "DISRUPTED"
    if rollup in ("clear", "agree_clear", "clear_gtfs_only"):
        return "NORMAL"
    return "UNKNOWN"


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

        maps_expected = route["scan_source"] in ("maps", "both")
        gtfs_expected = route["scan_source"] in ("gtfs", "both")
        route_dict["maps_expected"] = maps_expected
        route_dict["gtfs_expected"] = gtfs_expected

        gtfs_scan_days = (
            [int(x) for x in route["gtfs_scan_days"].split(",")]
            if route["gtfs_scan_days"]
            else route_dict["scan_days"]
        )
        route_dict["effective_scan_days"] = sorted(set(route_dict["scan_days"]) | set(gtfs_scan_days))

        gtfs_lookahead_weeks = (
            route["gtfs_lookahead_weeks"] if route["gtfs_lookahead_weeks"] is not None else route["lookahead_weeks"]
        )
        window_weeks = max(route["lookahead_weeks"], gtfs_lookahead_weeks)

        # Baseline is pinned to the Maps source specifically — this is what
        # `_derive_issues`'s duration-delta comparison expects. A GTFS-side duration
        # comparison isn't needed for the missing-trip/reduced-service signal itself.
        baseline = db.execute(
            """SELECT outbound_leg1_duration_s, outbound_leg2_duration_s,
                      return_leg1_duration_s, return_leg2_duration_s
               FROM baselines WHERE route_id = ? AND source = 'maps'""",
            (route["id"],),
        ).fetchone()
        baseline_dict = dict(baseline) if baseline else {}

        scan_rows = db.execute(
            """SELECT target_date, direction, leg, status, duration_s, steps, disruption_reasons,
                      scanned_at, source
               FROM scan_results WHERE route_id = ?
               AND target_date >= date('now')
               AND target_date <= date('now', ? || ' days')
               ORDER BY target_date, direction, leg""",
            (route["id"], window_weeks * 7),
        ).fetchall()

        leg_label_map = {lbl["key"]: lbl["label"] for lbl in route_dict["leg_labels"]}

        # Group raw rows by (target_date, leg_key, source) so the Maps and GTFS rows for the
        # same leg/date don't overwrite each other.
        grouped: dict[str, dict[str, dict]] = defaultdict(dict)
        for r in scan_rows:
            key = f"{r['direction']}_{r['leg']}"
            grouped[r["target_date"]].setdefault(key, {})[r["source"]] = r

        per_day: dict[str, dict] = {}
        by_date: dict[str, dict] = defaultdict(dict)

        for target_date, legs_by_key in grouped.items():
            per_day[target_date] = {"status": None, "legs": []}
            for key, by_source in legs_by_key.items():
                maps_row = by_source.get("maps")
                gtfs_row = by_source.get("gtfs")
                maps_status = _leg_status(maps_row)
                gtfs_status = _leg_status(gtfs_row)
                rollup = leg_rollup_status(maps_status, gtfs_status, maps_expected)

                reasons = []
                steps = []
                for row in (maps_row, gtfs_row):
                    if row is None:
                        continue
                    reasons.extend(json.loads(row["disruption_reasons"] or "[]"))
                    steps.extend(json.loads(row["steps"] or "[]"))

                duration_s = maps_row["duration_s"] if maps_row is not None else (
                    gtfs_row["duration_s"] if gtfs_row is not None else None
                )
                bl_key = f"{key.split('_')[0]}_leg{key.split('_')[1]}_duration_s"
                bl_dur = baseline_dict.get(bl_key)
                issues = _derive_issues(reasons, steps, bl_dur, duration_s)

                by_date[target_date][key] = {
                    "maps": {
                        "status": maps_status,
                        "duration_s": maps_row["duration_s"] if maps_row is not None else None,
                        "disruption_reasons": json.loads(maps_row["disruption_reasons"] or "[]") if maps_row is not None else [],
                        "scanned_at": maps_row["scanned_at"] if maps_row is not None else None,
                    } if maps_row is not None else None,
                    "gtfs": {
                        "status": gtfs_status,
                        "duration_s": gtfs_row["duration_s"] if gtfs_row is not None else None,
                        "disruption_reasons": json.loads(gtfs_row["disruption_reasons"] or "[]") if gtfs_row is not None else [],
                        "scanned_at": gtfs_row["scanned_at"] if gtfs_row is not None else None,
                    } if gtfs_row is not None else None,
                }

                per_day[target_date]["legs"].append({
                    "key": key,
                    "label": leg_label_map.get(key, key),
                    "status": _display_status(rollup),
                    "rollup": rollup,
                    "maps_status": maps_status,
                    "gtfs_status": gtfs_status,
                    "duration_s": duration_s,
                    "issues": issues,
                    "reasons": reasons,
                })

        for day in per_day.values():
            day["status"] = day_rollup_status([leg["rollup"] for leg in day["legs"]])

        route_dict["disrupted_day_count"] = sum(
            1 for d in per_day.values() if d["status"] == "disrupted"
        )
        route_dict["disagreement_day_count"] = sum(
            1 for d in per_day.values() if d["status"] == "disagree"
        )

        scan_days_set = set(route_dict["effective_scan_days"])
        lookahead_end = today + timedelta(days=window_weeks * 7)
        first_clear = None
        cur = today + timedelta(days=1)
        while cur <= lookahead_end:
            if cur.weekday() in scan_days_set:
                ds = cur.isoformat()
                if ds in per_day and per_day[ds]["status"] in ("clear", "clear_gtfs_only"):
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
    route = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if not route:
        db.close()
        raise HTTPException(status_code=404, detail="Route not found")

    # #17: both sources' rows are returned (not filtered to one), each tagged with its own
    # `source` and a `key` that includes it — avoids the Maps/GTFS key collision from #16's
    # holding fix, and gives #18's modal both sources' raw data for the same leg/date.
    scan_rows = db.execute(
        "SELECT * FROM scan_results WHERE route_id = ? ORDER BY target_date, direction, leg, source",
        (route_id,),
    ).fetchall()
    db.close()

    results = []
    for r in scan_rows:
        results.append({
            "target_date": r["target_date"],
            "direction": r["direction"],
            "leg": r["leg"],
            "source": r["source"],
            "key": f"{r['direction']}_{r['leg']}_{r['source']}",
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
