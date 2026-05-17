"""
Orchestrates baseline capture and disruption scanning.
"""

import datetime
import json
import time
from zoneinfo import ZoneInfo

from database import get_db
from routes_api import compute_all_routes, parse_duration_s, parse_transit_steps
from stations import get_coords

LONDON = ZoneInfo("Europe/London")
SCAN_SLOT = "12:00"
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


def _fetch_leg_options(origin_crs: str, dest_crs: str, baseline_date: str, route_id: int) -> list[dict]:
    """Query origin→dest at noon ±0/30/60 min, return deduplicated journey options."""
    origin_lat, origin_lon = get_coords(origin_crs)
    dest_lat, dest_lon = get_coords(dest_crs)
    seen: set[tuple] = set()
    options = []
    for offset in (0, 30, 60):
        departure_iso = _to_utc_iso(baseline_date, _add_minutes(SCAN_SLOT, offset))
        routes = compute_all_routes(
            origin_lat, origin_lon, dest_lat, dest_lon,
            departure_iso, route_id, "baseline_preview",
        )
        for route in routes:
            steps = parse_transit_steps(route)
            if not steps:
                continue
            key = tuple((s["dep_stop"], s["arr_stop"]) for s in steps)
            if key not in seen:
                seen.add(key)
                options.append({
                    "duration_s": parse_duration_s(route),
                    "steps": steps,
                    "dep_stop": steps[0]["dep_stop"],
                    "arr_stop": steps[-1]["arr_stop"],
                })
        time.sleep(0.3)
    return options


def fetch_baseline_options(route_id: int, baseline_date: str) -> dict:
    db = get_db()
    row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    db.close()
    if not row:
        raise ValueError(f"Route {route_id} not found")

    origin_crs = row["origin_crs"]
    change_crs = row["change_crs"]
    dest_crs = row["destination_crs"]
    has_change = bool(change_crs)

    result = {}
    for direction in ("outbound", "return"):
        a = origin_crs if direction == "outbound" else dest_crs
        b = dest_crs if direction == "outbound" else origin_crs

        if has_change:
            leg1 = _fetch_leg_options(a, change_crs, baseline_date, route_id)
            leg2 = _fetch_leg_options(change_crs, b, baseline_date, route_id)
        else:
            leg1 = _fetch_leg_options(a, b, baseline_date, route_id)
            leg2 = None

        result[direction] = {"leg1": leg1, "leg2": leg2}

    return result


def confirm_baseline(route_id: int, baseline_date: str, selections: dict) -> None:
    db = get_db()
    try:
        if not db.execute("SELECT id FROM routes WHERE id = ?", (route_id,)).fetchone():
            raise ValueError(f"Route {route_id} not found")

        def _vals(sel):
            if sel is None:
                return None, None, None, None
            return (
                sel.get("duration_s"),
                json.dumps(sel.get("steps", [])),
                sel.get("dep_stop") or None,
                sel.get("arr_stop") or None,
            )

        ol1_dur, ol1_steps, ol1_dep, ol1_arr = _vals(selections.get("outbound_leg1"))
        ol2_dur, ol2_steps, ol2_dep, ol2_arr = _vals(selections.get("outbound_leg2"))
        rl1_dur, rl1_steps, rl1_dep, rl1_arr = _vals(selections.get("return_leg1"))
        rl2_dur, rl2_steps, rl2_dep, rl2_arr = _vals(selections.get("return_leg2"))

        db.execute(
            """INSERT OR REPLACE INTO baselines
               (route_id, baseline_date,
                outbound_leg1_duration_s, outbound_leg1_steps, outbound_leg1_dep_stop, outbound_leg1_arr_stop,
                outbound_leg2_duration_s, outbound_leg2_steps, outbound_leg2_dep_stop, outbound_leg2_arr_stop,
                return_leg1_duration_s,   return_leg1_steps,   return_leg1_dep_stop,   return_leg1_arr_stop,
                return_leg2_duration_s,   return_leg2_steps,   return_leg2_dep_stop,   return_leg2_arr_stop)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (route_id, baseline_date,
             ol1_dur, ol1_steps, ol1_dep, ol1_arr,
             ol2_dur, ol2_steps, ol2_dep, ol2_arr,
             rl1_dur, rl1_steps, rl1_dep, rl1_arr,
             rl2_dur, rl2_steps, rl2_dep, rl2_arr),
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

    origin_crs = row["origin_crs"]
    change_crs = row["change_crs"]
    dest_crs = row["destination_crs"]
    has_change = bool(change_crs)
    threshold_pct = row["threshold_pct"]
    scan_days = [int(d) for d in row["scan_days"].split(",")]

    # (direction, leg, origin_crs, dest_crs, baseline_dep_stop, baseline_arr_stop, baseline_duration_s)
    leg_configs = [
        ("outbound", 1,
         origin_crs, change_crs if has_change else dest_crs,
         baseline["outbound_leg1_dep_stop"],
         baseline["outbound_leg1_arr_stop"],
         baseline["outbound_leg1_duration_s"]),
    ]
    if has_change:
        leg_configs.append(("outbound", 2,
            change_crs, dest_crs,
            baseline["outbound_leg2_dep_stop"],
            baseline["outbound_leg2_arr_stop"],
            baseline["outbound_leg2_duration_s"]))
    leg_configs.append(("return", 1,
        dest_crs, change_crs if has_change else origin_crs,
        baseline["return_leg1_dep_stop"],
        baseline["return_leg1_arr_stop"],
        baseline["return_leg1_duration_s"]))
    if has_change:
        leg_configs.append(("return", 2,
            change_crs, origin_crs,
            baseline["return_leg2_dep_stop"],
            baseline["return_leg2_arr_stop"],
            baseline["return_leg2_duration_s"]))

    today = datetime.date.today()
    end_date = today + datetime.timedelta(weeks=row["lookahead_weeks"])
    target_dates = [
        today + datetime.timedelta(days=i)
        for i in range((end_date - today).days + 1)
        if (today + datetime.timedelta(days=i)).weekday() in scan_days
    ]

    counts = {"NORMAL": 0, "DISRUPTED": 0, "UNKNOWN": 0}
    for target_date in target_dates:
        date_str = target_date.isoformat()
        for direction, leg, o_crs, d_crs, bl_dep_stop, bl_arr_stop, bl_duration_s in leg_configs:
            origin_lat, origin_lon = get_coords(o_crs)
            dest_lat, dest_lon = get_coords(d_crs)
            _query_and_compare(
                route_id,
                (origin_lat, origin_lon), (dest_lat, dest_lon),
                bl_dep_stop, bl_arr_stop, date_str, direction, leg,
                bl_duration_s, threshold_pct,
            )
            status = _last_status(route_id, date_str, direction, leg)
            if status in counts:
                counts[status] += 1
            time.sleep(0.2)

    db = get_db()
    try:
        db.execute("UPDATE routes SET last_scanned_at = datetime('now') WHERE id = ?", (route_id,))
        db.commit()
    finally:
        db.close()
    return {"route_id": route_id, "dates_scanned": len(target_dates), "counts": counts}


def _last_status(route_id: int, target_date: str, direction: str, leg: int) -> str:
    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM scan_results WHERE route_id=? AND target_date=? AND direction=? AND leg=?",
            (route_id, target_date, direction, leg),
        ).fetchone()
    finally:
        db.close()
    return row["status"] if row else "UNKNOWN"


def _query_and_compare(
    route_id: int,
    origin: tuple,
    dest: tuple,
    baseline_dep_stop: str | None,
    baseline_arr_stop: str,
    target_date: str,
    direction: str,
    leg: int,
    baseline_duration_s: int | None,
    threshold_pct: int,
) -> None:
    if not baseline_arr_stop:
        _save_result(route_id, target_date, direction, leg, "UNKNOWN", None, [], ["No baseline arrival stop recorded"])
        return

    found_direct = None       # single RAIL step, correct dep+arr
    found_direct_bus = None   # single BUS step, correct dep+arr
    got_any_routes = False

    for offset in (0, 30, 60, 90, 120):
        if offset > 0:
            time.sleep(0.2)
        departure_iso = _to_utc_iso(target_date, _add_minutes(SCAN_SLOT, offset))
        routes = compute_all_routes(
            origin[0], origin[1], dest[0], dest[1],
            departure_iso, route_id, "scan",
        )
        if routes:
            got_any_routes = True
        for route in routes:
            steps = parse_transit_steps(route)
            # Any change (multiple legs) means no direct service — skip entirely
            if len(steps) != 1:
                continue
            step = steps[0]
            # dep_stop must match baseline if we have one (skip if departing from wrong station)
            if baseline_dep_stop and step["dep_stop"] != baseline_dep_stop:
                continue
            if step["arr_stop"] != baseline_arr_stop:
                continue
            if step["vehicle_type"] in RAIL_VEHICLE_TYPES:
                found_direct = (route, steps)
                break
            if step["vehicle_type"] in BUS_VEHICLE_TYPES and found_direct_bus is None:
                found_direct_bus = (route, steps)
        if found_direct:
            break

    if found_direct is None:
        if not got_any_routes:
            _save_result(route_id, target_date, direction, leg, "UNKNOWN", None, [], ["No routes returned by API"])
        elif found_direct_bus is not None:
            _save_result(route_id, target_date, direction, leg, "DISRUPTED",
                         None, found_direct_bus[1], ["Rail replacement bus detected"])
        else:
            _save_result(route_id, target_date, direction, leg, "DISRUPTED", None, [], ["No direct service found"])
        return

    route, steps = found_direct
    duration_s = parse_duration_s(route)
    reasons = []
    if baseline_duration_s and duration_s:
        threshold = baseline_duration_s * (1 + threshold_pct / 100)
        if duration_s > threshold:
            pct_over = round((duration_s / baseline_duration_s - 1) * 100)
            reasons.append(
                f"Journey {pct_over}% longer than baseline "
                f"({_fmt_duration(duration_s)} vs {_fmt_duration(baseline_duration_s)})"
            )
    status = "DISRUPTED" if reasons else "NORMAL"
    _save_result(route_id, target_date, direction, leg, status, duration_s, steps, reasons)


def _save_result(
    route_id: int,
    target_date: str,
    direction: str,
    leg: int,
    status: str,
    duration_s: int | None,
    steps: list,
    reasons: list,
) -> None:
    db = get_db()
    try:
        db.execute(
            """INSERT OR REPLACE INTO scan_results
               (route_id, target_date, direction, leg, status, duration_s, steps, disruption_reasons)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (route_id, target_date, direction, leg, status,
             duration_s, json.dumps(steps), json.dumps(reasons)),
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
