"""
Google Maps Routes API v2 client.
Reuses the request pattern from explore_routes_api.py.
"""

import json
import os
import urllib.request
import urllib.error

_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
_ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"
_FIELD_MASK = (
    "routes.duration,"
    "routes.legs.steps.travelMode,"
    "routes.legs.steps.transitDetails.stopDetails,"
    "routes.legs.steps.transitDetails.transitLine.vehicle,"
    "routes.legs.steps.transitDetails.transitLine.name,"
    "routes.legs.steps.transitDetails.transitLine.agencies"
)


def _latlng(lat: float, lon: float) -> dict:
    return {"location": {"latLng": {"latitude": lat, "longitude": lon}}}


def _call_api(body: dict, route_id: int | None, purpose: str) -> list[dict]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _API_KEY,
        "X-Goog-FieldMask": _FIELD_MASK,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(_ENDPOINT, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError:
        return []
    _log_usage(route_id, purpose)
    return result.get("routes", [])


def _transit_body(
    origin_lat: float, origin_lon: float,
    dest_lat: float, dest_lon: float,
    departure_iso: str,
    alternatives: bool,
) -> dict:
    # Note: TRANSIT mode does not support intermediates in Routes API v2;
    # waypoints are stored in CRS sequence for display/monitoring logic only.
    return {
        "origin": _latlng(origin_lat, origin_lon),
        "destination": _latlng(dest_lat, dest_lon),
        "travelMode": "TRANSIT",
        "transitPreferences": {"routingPreference": "FEWER_TRANSFERS"},
        "departureTime": departure_iso,
        "computeAlternativeRoutes": alternatives,
    }


def compute_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    waypoints: list[tuple[float, float]],
    departure_iso: str,
    route_id: int | None = None,
    purpose: str = "scan",
) -> dict | None:
    body = _transit_body(origin_lat, origin_lon, dest_lat, dest_lon, departure_iso, False)
    routes = _call_api(body, route_id, purpose)
    return routes[0] if routes else None


def compute_all_routes(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    departure_iso: str,
    route_id: int | None = None,
    purpose: str = "baseline_preview",
) -> list[dict]:
    body = _transit_body(origin_lat, origin_lon, dest_lat, dest_lon, departure_iso, True)
    return _call_api(body, route_id, purpose)


def parse_duration_s(route: dict) -> int | None:
    raw = route.get("duration")
    if not raw:
        return None
    return int(raw.rstrip("s"))


def parse_transit_steps(route: dict) -> list[dict]:
    steps = []
    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            if step.get("travelMode") != "TRANSIT":
                continue
            td = step.get("transitDetails", {})
            stop_details = td.get("stopDetails", {})
            line = td.get("transitLine", {})
            vehicle = line.get("vehicle", {})
            agencies = line.get("agencies", [{}])
            steps.append({
                "dep_stop": stop_details.get("departureStop", {}).get("name", ""),
                "arr_stop": stop_details.get("arrivalStop", {}).get("name", ""),
                "vehicle_type": vehicle.get("type", "UNKNOWN"),
                "operator": agencies[0].get("name", "") if agencies else "",
                "line_name": line.get("name", ""),
            })
    return steps


def _log_usage(route_id: int | None, purpose: str) -> None:
    try:
        from database import get_db
        db = get_db()
        db.execute(
            "INSERT INTO api_usage_log (route_id, purpose) VALUES (?, ?)",
            (route_id, purpose),
        )
        db.commit()
        db.close()
    except Exception:
        pass
