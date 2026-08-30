"""
Orchestrates baseline capture and disruption scanning against the TfL Unified API.
"""

import datetime
import json
import logging
from datetime import date, time, timedelta

import tfl_client
from database import get_db

TFL_SEARCH_WINDOW_MINUTES = 120
TFL_MAX_CALLS_PER_DATE_DIRECTION = 8

logger = logging.getLogger(__name__)


def scan_window_end_date(today: date | None = None) -> date:
    """Last calendar day of *next* month from today -- current month + next month, per issue
    #24 follow-up (replaces the old per-route lookahead_weeks knob with a fixed, app-wide
    window). E.g. today=2026-08-30 -> 2026-09-30."""
    today = today or date.today()
    month_after_next = today.month + 2
    year = today.year + (month_after_next - 1) // 12
    month = (month_after_next - 1) % 12 + 1
    first_of_month_after_next = date(year, month, 1)
    return first_of_month_after_next - timedelta(days=1)


def _target_dates() -> list[date]:
    """Every day from today through scan_window_end_date() -- scanning is no longer limited to
    specific weekdays (issue #24 follow-up: scan_days removed, every day is scanned)."""
    today = date.today()
    end_date = scan_window_end_date(today)
    return [today + timedelta(days=i) for i in range((end_date - today).days + 1)]


def _itinerary_to_dict(it: tfl_client.Itinerary) -> dict:
    return {
        "duration_s": it.duration_s,
        "interchange_stops": it.interchange_stops,
        "leg_modes": it.leg_modes,
        "steps": it.steps,
        "usable": it.usable,
    }


# --- Baseline capture flow -------------------------------------------------

def fetch_baseline_options(route_id: int, baseline_date: str) -> dict:
    """One call to fetch_journeys per direction at noon on baseline_date (not paginated --
    baseline capture is a one-time human-reviewed action). Returns
    {"outbound": [itinerary_dict, ...], "return": [itinerary_dict, ...]}. Itineraries with
    usable=False are hidden from the picker."""
    db = get_db()
    row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    db.close()
    if not row:
        raise ValueError(f"Route {route_id} not found")

    result = {}
    for direction in ("outbound", "return"):
        origin, dest = (
            (row["origin_stop_id"], row["destination_stop_id"]) if direction == "outbound"
            else (row["destination_stop_id"], row["origin_stop_id"])
        )
        slot = row["departure_time"] if direction == "outbound" else row["return_time"]
        r = tfl_client.fetch_journeys(origin, dest, baseline_date, slot, route_id, "baseline_preview")
        if not r.ok:
            result[direction] = []
            continue
        result[direction] = [_itinerary_to_dict(it) for it in r.itineraries if it.usable]
    return result


def confirm_baseline(route_id: int, baseline_date: str, outbound: dict, return_: dict) -> None:
    """Single transaction: INSERT OR REPLACE both direction rows, each snapshotting the
    route's CURRENT origin_stop_id/destination_stop_id."""
    db = get_db()
    try:
        row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        if not row:
            raise ValueError(f"Route {route_id} not found")

        rows = [
            ("outbound", row["origin_stop_id"], row["destination_stop_id"], outbound),
            ("return", row["destination_stop_id"], row["origin_stop_id"], return_),
        ]
        for direction, origin_stop_id, destination_stop_id, sel in rows:
            db.execute(
                """INSERT OR REPLACE INTO baselines
                   (route_id, baseline_date, direction, origin_stop_id, destination_stop_id,
                    duration_s, interchange_stops, leg_modes, steps)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    route_id, baseline_date, direction, origin_stop_id, destination_stop_id,
                    sel["duration_s"], json.dumps(sel["interchange_stops"]),
                    json.dumps(sel["leg_modes"]), json.dumps(sel["steps"]),
                ),
            )
        db.commit()
    finally:
        db.close()


def _get_baseline(route_id: int, direction: str):
    db = get_db()
    try:
        return db.execute(
            "SELECT * FROM baselines WHERE route_id = ? AND direction = ?",
            (route_id, direction),
        ).fetchone()
    finally:
        db.close()


# --- Detection ---------------------------------------------------------

def _itinerary_matches(baseline, candidate: tfl_client.Itinerary, threshold_pct: int) -> bool:
    """NORMAL iff:
    1. candidate.usable is True -- an itinerary with an unresolvable naptanId is never a match
    2. candidate has at least one transit leg -- a walking-only "journey" never matches
    3. candidate.interchange_stops == baseline's (order-sensitive, transit legs only)
    4. candidate.leg_modes == baseline's (transit legs only -- any leg becoming
       'bus'/'replacement-bus' fails even if station sequence/duration otherwise line up)
    5. candidate.duration_s <= baseline.duration_s * (1 + threshold_pct / 100)
    All five structural. disruptions[] text never drives this."""
    if not candidate.usable:
        return False
    if not candidate.leg_modes:
        return False
    baseline_interchange = json.loads(baseline["interchange_stops"])
    baseline_leg_modes = json.loads(baseline["leg_modes"])
    if candidate.interchange_stops != baseline_interchange:
        return False
    if candidate.leg_modes != baseline_leg_modes:
        return False
    threshold = baseline["duration_s"] * (1 + threshold_pct / 100)
    if candidate.duration_s > threshold:
        return False
    return True


def _filter_display_reasons(disruptions: list[dict]) -> list[str]:
    reasons = []
    for d in disruptions:
        if d.get("category") == "Information":
            continue
        text = d.get("description") or d.get("summary") or d.get("category") or ""
        if text:
            reasons.append(text)
    return reasons


def _scan_direction(route, baseline, target_date: str, direction: str) -> dict:
    origin, dest = (
        (route["origin_stop_id"], route["destination_stop_id"]) if direction == "outbound"
        else (route["destination_stop_id"], route["origin_stop_id"])
    )

    if (baseline["origin_stop_id"], baseline["destination_stop_id"]) != (origin, dest):
        return {"status": "UNKNOWN", "reasons": ["baseline captured for different stations -- recapture required"],
                "calls_made": 0, "window_fully_walked": False}

    slot = route["departure_time"] if direction == "outbound" else route["return_time"]
    query_dt = datetime.datetime.combine(date.fromisoformat(target_date), time.fromisoformat(slot))
    window_end = query_dt + timedelta(minutes=TFL_SEARCH_WINDOW_MINUTES)

    calls_made = 0
    best_match = None
    fastest_seen = None
    window_fully_walked = False
    no_data_hit = False

    while calls_made < TFL_MAX_CALLS_PER_DATE_DIRECTION:
        result = tfl_client.fetch_journeys_at(origin, dest, query_dt, route["id"], "scan")
        calls_made += 1
        if not result.ok:
            break
        if result.no_data:
            # Only expected past TfL's ~104-day horizon (decision #8) -- never treated as a
            # genuine zero-itinerary disruption.
            no_data_hit = True
            break

        in_window = [it for it in result.itineraries if it.departure_dt <= window_end]
        max_departure = max((it.departure_dt for it in result.itineraries), default=None)

        for it in in_window:
            if it.usable and (fastest_seen is None or it.duration_s < fastest_seen.duration_s):
                fastest_seen = it
            if _itinerary_matches(baseline, it, route["threshold_pct"]):
                best_match = it
                break
        if best_match is not None:
            break
        if max_departure is None or max_departure >= window_end:
            window_fully_walked = True
            break
        query_dt = max_departure + timedelta(minutes=1)

    if best_match is not None:
        return {
            "status": "NORMAL", "duration_s": best_match.duration_s,
            "matched_steps": best_match.steps, "calls_made": calls_made,
            "window_fully_walked": True,
        }

    if not window_fully_walked:
        reason = (
            "TfL reports no data for this date (beyond lookahead horizon)" if no_data_hit
            else f"TfL API error or call-cap reached after {calls_made} call(s), window not fully checked"
        )
        return {
            "status": "UNKNOWN", "reasons": [reason],
            "calls_made": calls_made, "window_fully_walked": False,
        }

    reasons = _filter_display_reasons(fastest_seen.disruptions if fastest_seen else [])
    return {
        "status": "DISRUPTED",
        "duration_s": fastest_seen.duration_s if fastest_seen else None,
        "alternate_steps": fastest_seen.steps if fastest_seen else None,
        "disruption_reasons": reasons, "calls_made": calls_made, "window_fully_walked": True,
    }


def _save_result(route_id: int, target_date: str, direction: str, outcome: dict) -> None:
    db = get_db()
    try:
        db.execute(
            """INSERT OR REPLACE INTO scan_results
               (route_id, target_date, direction, status, duration_s, matched_steps,
                alternate_steps, disruption_reasons, calls_made, window_fully_walked)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                route_id, target_date, direction, outcome["status"],
                outcome.get("duration_s"),
                json.dumps(outcome["matched_steps"]) if outcome.get("matched_steps") is not None else None,
                json.dumps(outcome["alternate_steps"]) if outcome.get("alternate_steps") is not None else None,
                json.dumps(outcome.get("disruption_reasons") or outcome.get("reasons") or []),
                outcome.get("calls_made", 0),
                int(bool(outcome.get("window_fully_walked", False))),
            ),
        )
        db.commit()
    finally:
        db.close()


def scan_route(route_id: int) -> dict:
    db = get_db()
    row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    db.close()
    if not row:
        raise ValueError(f"Route {route_id} not found")

    outbound_baseline = _get_baseline(route_id, "outbound")
    return_baseline = _get_baseline(route_id, "return")
    if not outbound_baseline or not return_baseline:
        raise ValueError(f"No baseline captured for route {route_id}")

    target_dates = _target_dates()

    counts = {"NORMAL": 0, "DISRUPTED": 0, "UNKNOWN": 0}
    for target_date in target_dates:
        date_str = target_date.isoformat()
        for direction, baseline in (("outbound", outbound_baseline), ("return", return_baseline)):
            outcome = _scan_direction(row, baseline, date_str, direction)
            _save_result(route_id, date_str, direction, outcome)
            if outcome["status"] in counts:
                counts[outcome["status"]] += 1

    db = get_db()
    try:
        db.execute("UPDATE routes SET last_scanned_at = datetime('now') WHERE id = ?", (route_id,))
        db.commit()
    finally:
        db.close()
    return {"route_id": route_id, "dates_scanned": len(target_dates), "counts": counts}


def scan_all_routes() -> None:
    db = get_db()
    route_ids = [r["id"] for r in db.execute("SELECT id FROM routes").fetchall()]
    db.close()
    for route_id in route_ids:
        try:
            scan_route(route_id)
        except Exception as e:
            logger.error("Error scanning route %s: %s", route_id, e)
