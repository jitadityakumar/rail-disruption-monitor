"""
Orchestrates baseline capture and disruption scanning.
"""

import datetime
import json
import time
from zoneinfo import ZoneInfo

import gtfs_api
from database import get_db
from routes_api import compute_all_routes, parse_duration_s, parse_transit_steps
from stations import get_coords

LONDON = ZoneInfo("Europe/London")
SCAN_SLOT = "12:00"
RAIL_VEHICLE_TYPES = {"HEAVY_RAIL", "COMMUTER_TRAIN", "HIGH_SPEED_TRAIN", "LONG_DISTANCE_TRAIN", "INTERCITY"}
BUS_VEHICLE_TYPES = {"BUS", "INTERCITY_BUS", "TROLLEYBUS"}

# GTFS-side tuning. GTFS_WINDOW_MINUTES is 121, not 120: the sibling trims with a strict `<`
# against window_start+window_minutes (queries.py:744), so 120 would exclude a trip departing
# exactly at noon+120 — unlike the Maps offset loop's `offset in (0,30,60,90,120)`, which
# *includes* the +120 slot. 121 preserves the intended parity.
GTFS_WINDOW_MINUTES = 121
REDUCED_SERVICE_RATIO = 0.5   # placeholder — open question, tracked on #13; revisit empirically


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


def _leg_pairs(row, direction: str) -> list[tuple[str, str, str]]:
    """[(leg_key, origin_crs, dest_crs), ...] for a direction ('outbound'|'return')."""
    origin_crs = row["origin_crs"]
    change_crs = row["change_crs"]
    dest_crs = row["destination_crs"]
    has_change = bool(change_crs)
    a = origin_crs if direction == "outbound" else dest_crs
    b = dest_crs if direction == "outbound" else origin_crs
    if has_change:
        return [("leg1", a, change_crs), ("leg2", change_crs, b)]
    return [("leg1", a, b)]


def _target_dates(lookahead_weeks: int, scan_days: list[int]) -> list[datetime.date]:
    today = datetime.date.today()
    end_date = today + datetime.timedelta(weeks=lookahead_weeks)
    return [
        today + datetime.timedelta(days=i)
        for i in range((end_date - today).days + 1)
        if (today + datetime.timedelta(days=i)).weekday() in scan_days
    ]


def _nearest_date_for_weekday(base_date: datetime.date, weekday: int) -> datetime.date:
    delta = (weekday - base_date.weekday()) % 7
    return base_date + datetime.timedelta(days=delta)


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


# --- GTFS baseline flow -------------------------------------------------

def fetch_gtfs_baseline_options(route_id: int, baseline_date: str) -> dict:
    """Mirrors fetch_baseline_options's return shape ({direction: {leg1: [...], leg2: [...]}})
    but for GTFS. Only queries baseline_date itself (the admin picks one trip to confirm, same
    review-and-confirm UX as Maps). Per-weekday trip counts used for the reduced-service
    comparison are computed separately in confirm_gtfs_baseline, once the admin's picks are
    known, covering every distinct weekday in the route's effective GTFS scan-days."""
    db = get_db()
    row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    db.close()
    if not row:
        raise ValueError(f"Route {route_id} not found")

    has_change = bool(row["change_crs"])
    result = {}
    for direction in ("outbound", "return"):
        leg_result = {}
        for leg_key, o_crs, d_crs in _leg_pairs(row, direction):
            r = gtfs_api.fetch_gtfs_journeys(
                o_crs, d_crs, baseline_date, SCAN_SLOT, window_minutes=GTFS_WINDOW_MINUTES
            )
            direct = _direct_journeys(r.journeys) if r.ok else []
            leg_result[leg_key] = [{
                "duration_s": j["duration_minutes"] * 60,
                "departure_time": j["departure_time"],
                "intermediate_stops": j.get("direct", {}).get("intermediate_stops", []),
                "dep_stop": o_crs,
                "arr_stop": d_crs,
            } for j in direct]
        if not has_change:
            leg_result["leg2"] = None
        result[direction] = leg_result
    return result


def confirm_gtfs_baseline(route_id: int, baseline_date: str, selections: dict) -> None:
    """selections: same {outbound_leg1: {...}, ...} shape as confirm_baseline's selections arg,
    each with dep_stop/arr_stop/duration_s/departure_time/intermediate_stops (GtfsLegSelection).
    For each leg with a selection, computes trip_count_by_weekday by querying
    fetch_gtfs_direct_trips once per distinct weekday in the route's effective GTFS scan-days
    (nearest future occurrence of that weekday from baseline_date), then INSERT OR REPLACE into
    baselines with source='gtfs' explicitly named in the column list (never rely on the schema
    DEFAULT here — that would silently overwrite the route's Maps baseline)."""
    db = get_db()
    try:
        row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        if not row:
            raise ValueError(f"Route {route_id} not found")

        gtfs_scan_days_raw = row["gtfs_scan_days"] if row["gtfs_scan_days"] else row["scan_days"]
        distinct_weekdays = sorted({int(d) for d in gtfs_scan_days_raw.split(",")})
        base_date = datetime.date.fromisoformat(baseline_date)

        def _trip_count_by_weekday(o_crs: str, d_crs: str) -> dict:
            counts = {}
            for wd in distinct_weekdays:
                query_date = _nearest_date_for_weekday(base_date, wd)
                r = gtfs_api.fetch_gtfs_direct_trips(
                    o_crs, d_crs, query_date.isoformat(), SCAN_SLOT, window_minutes=GTFS_WINDOW_MINUTES
                )
                if r.ok:
                    counts[str(wd)] = len(r.journeys)
            return counts

        def _vals(sel):
            if sel is None:
                return None, None, None, None
            steps_obj = {
                "trip_count_by_weekday": _trip_count_by_weekday(sel["dep_stop"], sel["arr_stop"]),
                "departure_time": sel.get("departure_time"),
                "intermediate_stops": sel.get("intermediate_stops", []),
            }
            return (
                sel.get("duration_s"),
                json.dumps(steps_obj),
                sel.get("dep_stop") or None,
                sel.get("arr_stop") or None,
            )

        ol1_dur, ol1_steps, ol1_dep, ol1_arr = _vals(selections.get("outbound_leg1"))
        ol2_dur, ol2_steps, ol2_dep, ol2_arr = _vals(selections.get("outbound_leg2"))
        rl1_dur, rl1_steps, rl1_dep, rl1_arr = _vals(selections.get("return_leg1"))
        rl2_dur, rl2_steps, rl2_dep, rl2_arr = _vals(selections.get("return_leg2"))

        db.execute(
            """INSERT OR REPLACE INTO baselines
               (route_id, baseline_date, source,
                outbound_leg1_duration_s, outbound_leg1_steps, outbound_leg1_dep_stop, outbound_leg1_arr_stop,
                outbound_leg2_duration_s, outbound_leg2_steps, outbound_leg2_dep_stop, outbound_leg2_arr_stop,
                return_leg1_duration_s,   return_leg1_steps,   return_leg1_dep_stop,   return_leg1_arr_stop,
                return_leg2_duration_s,   return_leg2_steps,   return_leg2_dep_stop,   return_leg2_arr_stop)
               VALUES (?, ?, 'gtfs', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (route_id, baseline_date,
             ol1_dur, ol1_steps, ol1_dep, ol1_arr,
             ol2_dur, ol2_steps, ol2_dep, ol2_arr,
             rl1_dur, rl1_steps, rl1_dep, rl1_arr,
             rl2_dur, rl2_steps, rl2_dep, rl2_arr),
        )
        db.commit()
    finally:
        db.close()


# --- Scan dispatch --------------------------------------------------------

def _get_baseline(route_id: int, source: str):
    db = get_db()
    try:
        return db.execute(
            "SELECT * FROM baselines WHERE route_id = ? AND source = ?",
            (route_id, source),
        ).fetchone()
    finally:
        db.close()


def _merge_counts(into: dict, extra: dict) -> None:
    for k in into:
        into[k] += extra.get(k, 0)


def scan_route(route_id: int) -> dict:
    db = get_db()
    row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    db.close()
    if not row:
        raise ValueError(f"Route {route_id} not found")

    scan_source = row["scan_source"]
    counts = {"NORMAL": 0, "DISRUPTED": 0, "UNKNOWN": 0}
    dates_scanned = 0

    # Key asymmetry: missing Maps baseline is fatal (existing behavior, unchanged); missing
    # GTFS baseline is never fatal at the route level — every GTFS-side leg with no baseline
    # degrades to an UNKNOWN row (see _query_and_compare_gtfs's per-leg guard).
    if scan_source in ("maps", "both"):
        maps_baseline = _get_baseline(route_id, "maps")
        if not maps_baseline:
            raise ValueError(f"No baseline captured for route {route_id}")
        n, c = _scan_maps(row, maps_baseline)
        dates_scanned = max(dates_scanned, n)
        _merge_counts(counts, c)

    if scan_source in ("gtfs", "both"):
        gtfs_baseline = _get_baseline(route_id, "gtfs")   # may be None, or a row with some legs NULL
        n, c = _scan_gtfs(row, gtfs_baseline)
        dates_scanned = max(dates_scanned, n)
        _merge_counts(counts, c)

    db = get_db()
    try:
        db.execute("UPDATE routes SET last_scanned_at = datetime('now') WHERE id = ?", (route_id,))
        db.commit()
    finally:
        db.close()
    return {"route_id": route_id, "dates_scanned": dates_scanned, "counts": counts}


def _scan_maps(row, baseline) -> tuple[int, dict]:
    """Today's Maps scan behavior, unchanged — extracted so scan_route can call it
    conditionally on scan_source."""
    route_id = row["id"]
    origin_crs = row["origin_crs"]
    change_crs = row["change_crs"]
    dest_crs = row["destination_crs"]
    has_change = bool(change_crs)
    threshold_pct = row["threshold_pct"]
    scan_days = [int(d) for d in row["scan_days"].split(",")]

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

    target_dates = _target_dates(row["lookahead_weeks"], scan_days)

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
            status = _last_status(route_id, date_str, direction, leg, source="maps")
            if status in counts:
                counts[status] += 1
            time.sleep(0.2)
    return len(target_dates), counts


def _scan_gtfs(row, baseline) -> tuple[int, dict]:
    route_id = row["id"]
    origin_crs = row["origin_crs"]
    change_crs = row["change_crs"]
    dest_crs = row["destination_crs"]
    has_change = bool(change_crs)

    # Fix J: gtfs_lookahead_weeks needs an explicit `is None` check (so a stored 0 doesn't
    # silently inherit the Maps value); gtfs_scan_days needs a falsiness check, not `is None`,
    # since an empty string ("".split(",") -> [""] -> int("") crashes) is reachable via the API.
    gtfs_lookahead_weeks = row["gtfs_lookahead_weeks"] if row["gtfs_lookahead_weeks"] is not None else row["lookahead_weeks"]
    gtfs_scan_days_raw = row["gtfs_scan_days"] if row["gtfs_scan_days"] else row["scan_days"]
    gtfs_scan_days = [int(d) for d in gtfs_scan_days_raw.split(",")]

    def _leg_baseline(dur_col, steps_col, arr_col):
        if baseline is None:
            return None, None, None
        arr_stop = baseline[arr_col]
        duration_s = baseline[dur_col]
        trip_counts = None
        if baseline[steps_col]:
            try:
                trip_counts = json.loads(baseline[steps_col]).get("trip_count_by_weekday")
            except (ValueError, TypeError, AttributeError):
                trip_counts = None
        return arr_stop, trip_counts, duration_s

    ob1_arr, ob1_counts, ob1_dur = _leg_baseline("outbound_leg1_duration_s", "outbound_leg1_steps", "outbound_leg1_arr_stop")
    ob2_arr, ob2_counts, ob2_dur = _leg_baseline("outbound_leg2_duration_s", "outbound_leg2_steps", "outbound_leg2_arr_stop")
    rl1_arr, rl1_counts, rl1_dur = _leg_baseline("return_leg1_duration_s", "return_leg1_steps", "return_leg1_arr_stop")
    rl2_arr, rl2_counts, rl2_dur = _leg_baseline("return_leg2_duration_s", "return_leg2_steps", "return_leg2_arr_stop")

    leg_configs = [
        ("outbound", 1, origin_crs, change_crs if has_change else dest_crs, ob1_arr, ob1_counts, ob1_dur),
    ]
    if has_change:
        leg_configs.append(("outbound", 2, change_crs, dest_crs, ob2_arr, ob2_counts, ob2_dur))
    leg_configs.append(("return", 1, dest_crs, change_crs if has_change else origin_crs, rl1_arr, rl1_counts, rl1_dur))
    if has_change:
        leg_configs.append(("return", 2, change_crs, origin_crs, rl2_arr, rl2_counts, rl2_dur))

    target_dates = _target_dates(gtfs_lookahead_weeks, gtfs_scan_days)

    counts = {"NORMAL": 0, "DISRUPTED": 0, "UNKNOWN": 0}
    for target_date in target_dates:
        date_str = target_date.isoformat()
        for direction, leg, o_crs, d_crs, bl_arr_stop, bl_trip_counts, bl_duration_s in leg_configs:
            _query_and_compare_gtfs(
                route_id, o_crs, d_crs,
                bl_arr_stop, bl_trip_counts, bl_duration_s,
                date_str, direction, leg, gtfs_lookahead_weeks,
            )
            status = _last_status(route_id, date_str, direction, leg, source="gtfs")
            if status in counts:
                counts[status] += 1
    return len(target_dates), counts


def _weekday(date_str: str) -> int:
    return datetime.date.fromisoformat(date_str).weekday()


def _within_gtfs_feed_coverage(target_date: str, gtfs_lookahead_weeks: int) -> bool:
    """Interim fallback (sibling has no /health coverage-date field yet, tracked as a
    dependency): treat any date beyond today + gtfs_lookahead_weeks (the *configured* extended
    horizon) as inherently untrusted for the zero-result case."""
    today = datetime.date.today()
    horizon = today + datetime.timedelta(weeks=gtfs_lookahead_weeks)
    return datetime.date.fromisoformat(target_date) <= horizon


def _direct_journeys(journeys: list[dict]) -> list[dict]:
    return [j for j in journeys if j.get("kind") == "direct"]


def _query_and_compare_gtfs(
    route_id: int,
    origin_crs: str,
    dest_crs: str,
    baseline_arr_stop: str | None,
    baseline_trip_count_by_weekday: dict | None,
    baseline_duration_s: int | None,
    target_date: str,
    direction: str,
    leg: int,
    gtfs_lookahead_weeks: int,
) -> None:
    if not baseline_arr_stop:
        _save_result(route_id, target_date, direction, leg, "UNKNOWN", None, [],
                     ["no GTFS baseline captured for this leg"], source="gtfs")
        return

    result = gtfs_api.fetch_gtfs_journeys(
        origin_crs, dest_crs, target_date, SCAN_SLOT, window_minutes=GTFS_WINDOW_MINUTES
    )
    if not result.ok:
        _save_result(route_id, target_date, direction, leg, "UNKNOWN", None, [],
                     [f"GTFS API error: {result.error}"], source="gtfs")
        return

    # Distinguish "no scheduled service" (real disruption) from "date beyond the feed's own
    # calendar coverage" (would otherwise read as a permanent false DISRUPTED for every date
    # past the feed horizon — exactly what gtfs_lookahead_weeks exists to extend into).
    if len(result.journeys) == 0 and not _within_gtfs_feed_coverage(target_date, gtfs_lookahead_weeks):
        _save_result(route_id, target_date, direction, leg, "UNKNOWN", None, [],
                     ["date beyond GTFS feed coverage"], source="gtfs")
        return

    direct = _direct_journeys(result.journeys)
    if not direct:
        _save_result(route_id, target_date, direction, leg, "DISRUPTED", None, [],
                     ["no scheduled service found (GTFS)"], source="gtfs")
        return

    direct_trips_result = gtfs_api.fetch_gtfs_direct_trips(
        origin_crs, dest_crs, target_date, SCAN_SLOT, window_minutes=GTFS_WINDOW_MINUTES
    )
    # Fall back to the dominance-filtered count if /api/direct errors.
    trip_count = len(direct_trips_result.journeys) if direct_trips_result.ok else len(direct)
    baseline_count = (baseline_trip_count_by_weekday or {}).get(str(_weekday(target_date)))

    reasons = []
    if baseline_count and trip_count < baseline_count * REDUCED_SERVICE_RATIO:
        reasons.append(f"Only {trip_count} scheduled trip(s) vs baseline {baseline_count} (GTFS)")

    status = "DISRUPTED" if reasons else "NORMAL"
    duration_s = direct[0]["duration_minutes"] * 60
    intermediate_stops = direct[0].get("direct", {}).get("intermediate_stops", [])
    _save_result(route_id, target_date, direction, leg, status, duration_s, intermediate_stops, reasons, source="gtfs")


def _last_status(route_id: int, target_date: str, direction: str, leg: int, source: str = "maps") -> str:
    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM scan_results WHERE route_id=? AND target_date=? AND direction=? AND leg=? AND source=?",
            (route_id, target_date, direction, leg, source),
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
    source: str = "maps",
) -> None:
    db = get_db()
    try:
        db.execute(
            """INSERT OR REPLACE INTO scan_results
               (route_id, target_date, direction, leg, status, duration_s, steps, disruption_reasons, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (route_id, target_date, direction, leg, status,
             duration_s, json.dumps(steps), json.dumps(reasons), source),
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
