"""
GTFS journey-planner API client (self-hosted sibling service, see issue #13/#14).

Deliberately diverges from routes_api.py in error handling: a GtfsResult always tells the
caller whether the call succeeded, so a GTFS-side outage or malformed response can never be
silently read as "confirmed zero journeys" (see issue #13's "flag for manual check" wording).
Mirrors routes_api.py everywhere else (stdlib urllib, no new dependency).
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("GTFS_API_BASE_URL", "")
_MAX_WINDOW_MINUTES = 180
_TIMEOUT_S = 30


@dataclass(frozen=True)
class GtfsResult:
    ok: bool
    journeys: list[dict] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0


def check_health() -> bool:
    req = urllib.request.Request(f"{_BASE_URL}/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            body = json.loads(resp.read())
    except (OSError, ValueError) as e:
        logger.warning("GTFS health check failed: %s", e)
        return False
    if not isinstance(body, dict):
        logger.warning("GTFS health check returned non-object JSON body")
        return False
    return body.get("status") == "ok" and body.get("dataset_present") is True


def fetch_gtfs_journeys(
    origin_crs: str,
    dest_crs: str,
    date: str,
    time_: str,
    window_minutes: int = 60,
) -> GtfsResult:
    origin_crs = origin_crs.strip().upper()
    dest_crs = dest_crs.strip().upper()
    window_minutes = min(window_minutes, _MAX_WINDOW_MINUTES)

    params = urllib.parse.urlencode({
        "from": origin_crs,
        "to": dest_crs,
        "date": date,
        "time": time_,
        "window_minutes": window_minutes,
    })
    url = f"{_BASE_URL}/api/journeys?{params}"
    req = urllib.request.Request(url, method="GET")

    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "GTFS API HTTP %s for %s->%s on %s (likely bad/unknown CRS)",
            e.code, origin_crs, dest_crs, date,
        )
        return GtfsResult(ok=False, error=f"http_{e.code}", elapsed_ms=elapsed_ms)
    except OSError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "GTFS API network/timeout error for %s->%s on %s: %s",
            origin_crs, dest_crs, date, e,
        )
        return GtfsResult(ok=False, error="network_error", elapsed_ms=elapsed_ms)

    elapsed_ms = int((time.monotonic() - start) * 1000)

    try:
        body = json.loads(raw)
    except ValueError as e:
        logger.warning(
            "GTFS API returned malformed/non-JSON body for %s->%s on %s: %s",
            origin_crs, dest_crs, date, e,
        )
        return GtfsResult(ok=False, error="malformed_response", elapsed_ms=elapsed_ms)

    if not isinstance(body, dict):
        logger.warning(
            "GTFS API returned non-object JSON body for %s->%s on %s",
            origin_crs, dest_crs, date,
        )
        return GtfsResult(ok=False, error="malformed_response", elapsed_ms=elapsed_ms)

    if body.get("dataset_present") is False:
        logger.warning("GTFS API reports dataset_present=false for %s->%s on %s", origin_crs, dest_crs, date)
        return GtfsResult(ok=False, error="dataset_not_present", elapsed_ms=elapsed_ms)

    if "journeys" not in body:
        logger.warning(
            "GTFS API response missing 'journeys' key for %s->%s on %s",
            origin_crs, dest_crs, date,
        )
        return GtfsResult(ok=False, error="missing_journeys_key", elapsed_ms=elapsed_ms)

    return GtfsResult(ok=True, journeys=body["journeys"], error=None, elapsed_ms=elapsed_ms)
