"""
Orchestrates baseline capture and disruption scanning.
"""

import datetime
import json
import time
from zoneinfo import ZoneInfo

from database import get_db
from routes_api import compute_all_routes, compute_route, parse_duration_s, parse_transit_steps
from stations import get_coords

LONDON = ZoneInfo("Europe/London")
TIME_SLOTS = ["08:00", "13:00", "18:00"]
RAIL_VEHICLE_TYPES = {"HEAVY_RAIL", "COMMUTER_TRAIN", "HIGH_SPEED_TRAIN", "LONG_DISTANCE_TRAIN", "INTERCITY"}
BUS_VEHICLE_TYPES = {"BUS", "INTERCITY_BUS", "TROLLEYBUS"}


def _fmt_duration(seconds: int) -> str:
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _to_utc_iso(date_str: str, slot: str) -> str:
    hour, minute = slot.split(":")
    dt_local = datetime.datetime(
        *[int(x) for x in date_str.split("-")],
        int(hour), int(minute), 0,
        tzinfo=LONDON,
    )
    return dt_local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_minutes(slot: str, minutes: int) -> str:
    h, m = map(int, slot.split(":"))
    total = h * 60 + m + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


def _route_coords(crs_sequence: list[str]) -> tuple[tuple, tuple, list]:
    origin_lat, origin_lon = get_coords(crs_sequence[0])
    dest_lat, dest_lon = get_coords(crs_sequence[-1])
    waypoints = []
    for crs in crs_sequence[1:-1]:
        lat, lon = get_coords(crs)
        waypoints.append((lat, lon))
    return (origin_lat, origin_lon), (dest_lat, dest_lon), waypoints


def capture_baseline(route_id: int, baseline_date: str) -> dict:
    db = get_db()
    row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    db.close()
    if not row:
        raise ValueError(f"Route {route_id} not found")

    crs_sequence = json.loads(row["crs_sequence"])
    origin, dest, waypoints = _route_coords(crs_sequence)

    results = {}
    slot_data: dict[str, dict] = {}

    for slot in TIME_SLOTS:
        departure_iso = _to_utc_iso(baseline_date, slot)
        route = compute_route(
            origin[0], origin[1], dest[0], dest[1],
            waypoints, departure_iso, route_id, "baseline",
        )
        if route:
            duration_s = parse_duration_s(route)
            steps = parse_transit_steps(route)
            slot_data[slot] = {"duration_s": duration_s, "steps": steps}
            results[slot] = {"duration_s": duration_s, "steps": steps, "found": True}
        else:
            slot_data[slot] = {"duration_s": None, "steps": []}
            results[slot] = {"found": False}
        time.sleep(0.2)

    db = get_db()
    try:
        db.execute(
            """INSERT OR REPLACE INTO baselines
               (route_id, baseline_date,
                slot_08_duration_s, slot_13_duration_s, slot_18_duration_s,
                slot_08_steps, slot_13_steps, slot_18_steps)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                route_id, baseline_date,
                slot_data["08:00"]["duration_s"],
                slot_data["13:00"]["duration_s"],
                slot_data["18:00"]["duration_s"],
                json.dumps(slot_data["08:00"]["steps"]),
                json.dumps(slot_data["13:00"]["steps"]),
                json.dumps(slot_data["18:00"]["steps"]),
            ),
        )
        db.commit()
    finally:
        db.close()
    return results


def fetch_baseline_options(route_id: int, baseline_date: str) -> dict:
    db = get_db()
    row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    db.close()
    if not row:
        raise ValueError(f"Route {route_id} not found")

    crs_sequence = json.loads(row["crs_sequence"])
    origin, dest, _ = _route_coords(crs_sequence)

    result = {}
    for slot in TIME_SLOTS:
        seen: set[tuple] = set()
        options = []
        for offset in (0, 30, 60):
            departure_iso = _to_utc_iso(baseline_date, _add_minutes(slot, offset))
            routes = compute_all_routes(
                origin[0], origin[1], dest[0], dest[1],
                departure_iso, route_id, "baseline_preview",
            )
            for route in routes:
                steps = parse_transit_steps(route)
                key = tuple((s["dep_stop"], s["arr_stop"]) for s in steps)
                if key not in seen and steps:
                    seen.add(key)
                    options.append({"duration_s": parse_duration_s(route), "steps": steps})
            time.sleep(0.3)
        result[slot] = options
    return result


def confirm_baseline(route_id: int, baseline_date: str, selections: dict) -> None:
    db = get_db()
    try:
        if not db.execute("SELECT id FROM routes WHERE id = ?", (route_id,)).fetchone():
            raise ValueError(f"Route {route_id} not found")

        slot_data = {
            slot: selections.get(slot, {"duration_s": None, "steps": []})
            for slot in TIME_SLOTS
        }
        db.execute(
            """INSERT OR REPLACE INTO baselines
               (route_id, baseline_date,
                slot_08_duration_s, slot_13_duration_s, slot_18_duration_s,
                slot_08_steps, slot_13_steps, slot_18_steps)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                route_id, baseline_date,
                slot_data["08:00"]["duration_s"],
                slot_data["13:00"]["duration_s"],
                slot_data["18:00"]["duration_s"],
                json.dumps(slot_data["08:00"]["steps"]),
                json.dumps(slot_data["13:00"]["steps"]),
                json.dumps(slot_data["18:00"]["steps"]),
            ),
        )
        db.commit()
    finally:
        db.close()


def scan_route(route_id: int) -> dict:
    db = get_db()
    row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    baseline = db.execute("SELECT * FROM baselines WHERE route_id = ?", (route_id,)).fetchone()
    db.close()

    if not row:
        raise ValueError(f"Route {route_id} not found")
    if not baseline:
        raise ValueError(f"No baseline captured for route {route_id}")

    crs_sequence = json.loads(row["crs_sequence"])
    scan_days = [int(d) for d in row["scan_days"].split(",")]
    lookahead_weeks = row["lookahead_weeks"]
    threshold_pct = row["threshold_pct"]

    origin, dest, waypoints = _route_coords(crs_sequence)

    today = datetime.date.today()
    end_date = today + datetime.timedelta(weeks=lookahead_weeks)
    target_dates = [
        today + datetime.timedelta(days=i)
        for i in range((end_date - today).days + 1)
        if (today + datetime.timedelta(days=i)).weekday() in scan_days
    ]

    slot_baseline = {
        "08:00": {
            "duration_s": baseline["slot_08_duration_s"],
            "steps": json.loads(baseline["slot_08_steps"] or "[]"),
        },
        "13:00": {
            "duration_s": baseline["slot_13_duration_s"],
            "steps": json.loads(baseline["slot_13_steps"] or "[]"),
        },
        "18:00": {
            "duration_s": baseline["slot_18_duration_s"],
            "steps": json.loads(baseline["slot_18_steps"] or "[]"),
        },
    }

    counts = {"NORMAL": 0, "DISRUPTED": 0, "UNKNOWN": 0}
    for target_date in target_dates:
        date_str = target_date.isoformat()
        for slot in TIME_SLOTS:
            _query_and_compare(
                route_id, origin, dest, waypoints,
                date_str, slot, slot_baseline[slot], threshold_pct,
            )
            counts_key = _last_status(route_id, date_str, slot)
            if counts_key in counts:
                counts[counts_key] += 1
            time.sleep(0.2)

    db = get_db()
    try:
        db.execute(
            "UPDATE routes SET last_scanned_at = datetime('now') WHERE id = ?",
            (route_id,),
        )
        db.commit()
    finally:
        db.close()
    return {"route_id": route_id, "dates_scanned": len(target_dates), "counts": counts}


def _last_status(route_id: int, target_date: str, slot: str) -> str:
    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM scan_results WHERE route_id=? AND target_date=? AND time_slot=?",
            (route_id, target_date, slot),
        ).fetchone()
    finally:
        db.close()
    return row["status"] if row else "UNKNOWN"


def _query_and_compare(
    route_id: int,
    origin: tuple,
    dest: tuple,
    waypoints: list,
    target_date: str,
    slot: str,
    baseline: dict,
    threshold_pct: int,
) -> None:
    baseline_seq = [(s["dep_stop"], s["arr_stop"]) for s in baseline["steps"]]

    best_route = None
    best_steps = None
    for offset in (0, 30, 60, 90):
        if offset > 0:
            time.sleep(0.2)
        departure_iso = _to_utc_iso(target_date, _add_minutes(slot, offset))
        route = compute_route(
            origin[0], origin[1], dest[0], dest[1],
            waypoints, departure_iso, route_id, "scan",
        )
        if route is None:
            continue
        steps = parse_transit_steps(route)
        if best_route is None:
            best_route = route
            best_steps = steps
        if [(s["dep_stop"], s["arr_stop"]) for s in steps] == baseline_seq:
            best_route = route
            best_steps = steps
            break

    if best_route is None:
        _save_result(route_id, target_date, slot, "UNKNOWN", None, [], [])
        return

    duration_s = parse_duration_s(best_route)
    steps = best_steps
    reasons = []

    # Check 1: bus substitution
    baseline_rail = {(s["dep_stop"], s["arr_stop"]) for s in baseline["steps"] if s["vehicle_type"] in RAIL_VEHICLE_TYPES}
    for step in steps:
        if step["vehicle_type"] in BUS_VEHICLE_TYPES and (step["dep_stop"], step["arr_stop"]) in baseline_rail:
            reasons.append(f"Rail replacement bus on {step['dep_stop']} → {step['arr_stop']}")

    # Check 2: duration increase
    if duration_s and baseline["duration_s"]:
        threshold = baseline["duration_s"] * (1 + threshold_pct / 100)
        if duration_s > threshold:
            pct_over = round((duration_s / baseline["duration_s"] - 1) * 100)
            reasons.append(
                f"Journey {pct_over}% longer than baseline ({_fmt_duration(duration_s)} vs {_fmt_duration(baseline['duration_s'])})"
            )

    # Check 3: stop sequence change
    def stop_sequence(step_list):
        return [(s["dep_stop"], s["arr_stop"]) for s in step_list]

    if stop_sequence(steps) != stop_sequence(baseline["steps"]) and baseline["steps"]:
        if steps:
            stops = [steps[0]["dep_stop"]] + [s["arr_stop"] for s in steps]
            new_route = " → ".join(stops)
        else:
            new_route = "unknown"
        reasons.append(f"Route has changed: now {new_route}")

    status = "DISRUPTED" if reasons else "NORMAL"
    _save_result(route_id, target_date, slot, status, duration_s, steps, reasons)


def _save_result(
    route_id: int,
    target_date: str,
    slot: str,
    status: str,
    duration_s: int | None,
    steps: list,
    reasons: list,
) -> None:
    db = get_db()
    try:
        db.execute(
            """INSERT OR REPLACE INTO scan_results
               (route_id, target_date, time_slot, status, duration_s, steps, disruption_reasons)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (route_id, target_date, slot, status, duration_s, json.dumps(steps), json.dumps(reasons)),
        )
        db.commit()
    finally:
        db.close()


def scan_all_routes() -> None:
    db = get_db()
    route_ids = [r["id"] for r in db.execute("SELECT id FROM routes").fetchall()]
    db.close()
    for route_id in route_ids:
        try:
            scan_route(route_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Error scanning route %s: %s", route_id, e)
