"""
TfL Unified API (api.tfl.gov.uk) client -- replaces routes_api.py (Google Maps) and gtfs_api.py
(GTFS sibling service) per issue #24's rewrite.

Station resolution is via /StopPoint/Search (admin-driven, button-triggered), never a CRS/
proximity heuristic. Journey queries are via /Journey/JourneyResults/{from}/to/{to}, deliberately
WITHOUT `mode=`/`journeyPreference=` params (unlike the `roost` sibling project's commute-search
calls) -- those would filter out exactly the bus/replacement-bus legs this app's detection design
depends on (issue #24 plan, deviation A5). Reference implementation for the request/parsing
patterns this mirrors: `/home/jkumar/github/roost/backend/app/commute/tfl_client.py`.
"""
import datetime as dt
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_API_KEY = os.environ.get("TFL_API_KEY", "")
_TIMEOUT_SECONDS = 10
_HEADERS = {"User-Agent": "RailDisruptionMonitor/1.0 (+https://github.com/jitadityakumar/rail-disruption-monitor)"}
_SEARCH_MODES = "national-rail,tube,overground,dlr,tram,elizabeth-line"

_MAX_CALLS_PER_MINUTE = 400
_RATE_WINDOW_SECONDS = 60
_call_times: deque[float] = deque()
_throttle_lock = threading.Lock()


class TflApiError(Exception):
    pass


def _throttle(now: float | None = None) -> None:
    with _throttle_lock:
        now = time.monotonic() if now is None else now
        while _call_times and now - _call_times[0] > _RATE_WINDOW_SECONDS:
            _call_times.popleft()
        if len(_call_times) >= _MAX_CALLS_PER_MINUTE:
            sleep_for = _RATE_WINDOW_SECONDS - (now - _call_times[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
        _call_times.append(time.monotonic())


def _get(url: str) -> dict | list:
    sep = "&" if "?" in url else "?"
    full_url = f"{url}{sep}app_key={_API_KEY}"
    _throttle()
    req = Request(full_url, headers=_HEADERS, method="GET")
    try:
        with urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        raise TflApiError(f"http_{e.code}") from e
    except URLError as e:
        raise TflApiError(f"network_error: {e}") from e
    except TimeoutError as e:
        # A timeout during response-body read (after headers are already received) surfaces as
        # a bare TimeoutError/socket.timeout, not wrapped in URLError -- must be caught
        # separately or it propagates as an unhandled 500 instead of degrading to retry/ok=False.
        raise TflApiError(f"network_error: {e}") from e
    except ValueError as e:
        raise TflApiError(f"malformed_response: {e}") from e


def _log_usage(route_id: int | None, purpose: str) -> None:
    try:
        from database import get_db
        db = get_db()
        db.execute("INSERT INTO api_usage_log (route_id, purpose) VALUES (?, ?)", (route_id, purpose))
        db.commit()
        db.close()
    except Exception:
        logger.warning("Failed to log API usage", exc_info=True)


_SEARCH_MODE_SET = set(_SEARCH_MODES.split(","))


def search_stop_points(query: str, limit: int = 8) -> list[dict]:
    """GET /StopPoint/Search/{query}?modes=... -> [{"id","name","modes"}, ...].
    HUB ids are excluded from the returned list -- they're never selectable (RouteCreate's
    StopPoint model validator rejects them outright) and their concrete children are always
    expanded inline instead, so surfacing the HUB entry itself would just be a dead end in the
    UI. Never raises -- a failed/empty search just returns [].

    Hub children are expanded inline: found live that TfL's own /StopPoint/Search for
    "waterloo" returns ONLY the HUBWAT hub group, "London Waterloo East", and the unrelated
    "Waterloo (Merseyside)" -- the concrete "London Waterloo Rail Station" (the actual mainline
    station this app's routes need) never appears in the raw match list at all, so a hub-only
    result set left the admin with no valid station to pick for one of this app's two fixed
    routes. Fetching each hub's /StopPoint/{id} children (already used for the same purpose by
    stop_point_exists) and folding in the transit-mode ones fixes this without changing what a
    plain non-hub search returns."""
    query = query.strip()
    if not query:
        return []
    try:
        data = _get(f"https://api.tfl.gov.uk/StopPoint/Search/{quote(query)}?modes={_SEARCH_MODES}")
    except TflApiError as e:
        logger.info("TfL StopPoint/Search failed for %r: %s", query, e)
        return []

    matches = data.get("matches") or [] if isinstance(data, dict) else []
    results = []
    seen_ids = set()
    hub_ids = []
    for m in matches:
        stop_id = m.get("id")
        name = m.get("name")
        if not stop_id or not name or stop_id in seen_ids:
            continue
        seen_ids.add(stop_id)
        results.append({"id": stop_id, "name": name, "modes": m.get("modes") or []})
        if stop_id.upper().startswith("HUB"):
            hub_ids.append(stop_id)

    for hub_id in hub_ids:
        try:
            hub_data = _get(f"https://api.tfl.gov.uk/StopPoint/{quote(hub_id)}")
        except TflApiError:
            continue
        if not isinstance(hub_data, dict):
            continue
        for child in hub_data.get("children") or []:
            child_id = child.get("id")
            child_name = child.get("commonName")
            child_modes = child.get("modes") or []
            if not child_id or not child_name or child_id in seen_ids:
                continue
            if not any(mode in _SEARCH_MODE_SET for mode in child_modes):
                continue
            seen_ids.add(child_id)
            results.append({"id": child_id, "name": child_name, "modes": child_modes})

    results = [r for r in results if not r["id"].upper().startswith("HUB")]
    return results[:limit]


def stop_point_exists(stop_id: str) -> bool:
    """GET /StopPoint/{id} -- server-side existence check at route-creation time (issue #24
    plan §7 C1). Returns False on any error/HTTP failure, not just a clean 404."""
    try:
        data = _get(f"https://api.tfl.gov.uk/StopPoint/{quote(stop_id)}")
    except TflApiError:
        return False
    return isinstance(data, dict) and bool(data.get("id"))


def _leg_point_naptan_id(leg: dict, key: str) -> str | None:
    """naptanId first, id as fallback -- a leg's departurePoint/arrivalPoint.id is null on every
    real JourneyResults response; naptanId is the real identifier (A1)."""
    point = leg.get(key) or {}
    return point.get("naptanId") or point.get("id")


def _is_walking(leg: dict) -> bool:
    return (leg.get("mode") or {}).get("id") == "walking"


def _transit_legs(legs: list[dict]) -> list[dict]:
    """Excludes walking legs (A2) -- TfL itemizes an interchange walk as its own leg
    inconsistently; leg_modes/interchange_stops must be computed from this list only, or the
    same real journey can produce a different interchange_stops list on different days, causing
    intermittent false DISRUPTED against a baseline captured on a day TfL happened to (or
    didn't) itemize the walk."""
    return [leg for leg in legs if not _is_walking(leg)]


def _parse_tfl_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


_INFO_CATEGORY = "Information"


def _leg_summary(leg: dict) -> str:
    instr = leg.get("instruction") or {}
    return instr.get("summary") or ""


def _extract_itinerary(journey: dict) -> "Itinerary | None":
    legs = journey.get("legs") or []
    if not legs:
        return None
    duration_min = journey.get("duration")
    if duration_min is None:
        return None
    departure_dt = _parse_tfl_datetime(journey.get("startDateTime"))
    if departure_dt is None:
        return None

    transit = _transit_legs(legs)
    interchange_stops: list[str] = []
    leg_modes: list[str] = []
    usable = True

    for i, leg in enumerate(transit):
        mode_id = (leg.get("mode") or {}).get("id") or "unknown"
        leg_modes.append(mode_id)
        if i < len(transit) - 1:
            stop_id = _leg_point_naptan_id(leg, "arrivalPoint")
            if stop_id is None:
                usable = False
            interchange_stops.append(stop_id)

    steps = []
    for leg in legs:
        mode_id = (leg.get("mode") or {}).get("id") or "unknown"
        dep = leg.get("departurePoint") or {}
        arr = leg.get("arrivalPoint") or {}
        steps.append({
            "mode": mode_id,
            "dep_name": dep.get("commonName", ""),
            "dep_id": _leg_point_naptan_id(leg, "departurePoint"),
            "dep_time": leg.get("departureTime"),
            "arr_name": arr.get("commonName", ""),
            "arr_id": _leg_point_naptan_id(leg, "arrivalPoint"),
            "arr_time": leg.get("arrivalTime"),
            "summary": _leg_summary(leg),
        })

    disruptions = []
    for d in journey.get("disruptions") or []:
        if d.get("category") != _INFO_CATEGORY:
            disruptions.append(d)
    for leg in legs:
        for d in leg.get("disruptions") or []:
            if d.get("category") != _INFO_CATEGORY:
                disruptions.append(d)

    return Itinerary(
        duration_s=int(duration_min) * 60,
        interchange_stops=interchange_stops,
        leg_modes=leg_modes,
        departure_dt=departure_dt,
        steps=steps,
        disruptions=disruptions,
        usable=usable,
    )


@dataclass(frozen=True)
class Itinerary:
    duration_s: int
    interchange_stops: list[str]
    leg_modes: list[str]
    departure_dt: dt.datetime
    steps: list[dict]
    disruptions: list[dict]
    usable: bool


@dataclass(frozen=True)
class TflResult:
    ok: bool
    itineraries: list[Itinerary] = field(default_factory=list)
    error: str | None = None
    no_data: bool = False


def _fetch_journeys_raw(origin_stop_id: str, dest_stop_id: str, query_dt: dt.datetime,
                         route_id: int | None, purpose: str) -> TflResult:
    date_str = query_dt.strftime("%Y%m%d")
    time_str = query_dt.strftime("%H%M")
    url = (
        f"https://api.tfl.gov.uk/Journey/JourneyResults/{quote(origin_stop_id)}/to/"
        f"{quote(dest_stop_id)}?date={date_str}&time={time_str}&timeIs=Departing"
    )

    last_error = None
    for attempt in range(3):
        try:
            data = _get(url)
        except TflApiError as e:
            msg = str(e)
            if msg == "http_404":
                # Only expected past TfL's ~104-day lookahead horizon (issue #24 decision #8)
                # -- treated as "no data", never a genuine zero-itinerary disruption.
                return TflResult(ok=True, itineraries=[], no_data=True)
            if msg == "http_300":
                return TflResult(ok=False, error="ambiguous_stop_point")
            last_error = msg
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
            continue

        if not isinstance(data, dict) or "journeys" not in data:
            # HTTP 300 (ambiguous HUB) returns a dict without "journeys" -- same shape-check
            # roost applies.
            return TflResult(ok=False, error="ambiguous_stop_point")

        journeys = data.get("journeys") or []
        itineraries = [it for j in journeys if (it := _extract_itinerary(j)) is not None]
        _log_usage(route_id, purpose)
        return TflResult(ok=True, itineraries=itineraries)

    return TflResult(ok=False, error=last_error or "unknown_error")


def fetch_journeys(origin_stop_id: str, dest_stop_id: str, date: str, time_hhmm: str,
                    route_id: int | None = None, purpose: str = "scan") -> TflResult:
    """GET /Journey/JourneyResults/{origin}/to/{dest}?date=YYYYMMDD&time=HHMM&timeIs=Departing.
    date is 'YYYY-MM-DD', time_hhmm is 'HH:MM' (this app's convention) -- converted to TfL's
    %Y%m%d/%H%M here. 404 -> ok=True, itineraries=[] (only expected past TfL's ~104-day
    lookahead horizon; treated as "no data", never a genuine zero-itinerary disruption).
    Non-200/404/300 -> retry up to 2x with backoff, then ok=False."""
    y, m, d = date.split("-")
    hh, mm = time_hhmm.split(":")
    query_dt = dt.datetime(int(y), int(m), int(d), int(hh), int(mm))
    return _fetch_journeys_raw(origin_stop_id, dest_stop_id, query_dt, route_id, purpose)


def fetch_journeys_at(origin_stop_id: str, dest_stop_id: str, query_dt: dt.datetime,
                       route_id: int | None = None, purpose: str = "scan") -> TflResult:
    """Same as fetch_journeys but takes a full datetime -- used by the scanner's pagination
    re-query, which needs sub-minute-precision date/time advancement (max_departure + 1 minute)."""
    return _fetch_journeys_raw(origin_stop_id, dest_stop_id, query_dt, route_id, purpose)
