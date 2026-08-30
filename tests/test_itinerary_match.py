import datetime as dt
import json

import pytest

from scanner import _itinerary_matches
from tfl_client import Itinerary


def _baseline(interchange_stops=None, leg_modes=None, duration_s=1800):
    return {
        "interchange_stops": json.dumps(interchange_stops if interchange_stops is not None else []),
        "leg_modes": json.dumps(leg_modes if leg_modes is not None else ["national-rail"]),
        "duration_s": duration_s,
    }


def _itinerary(interchange_stops=None, leg_modes=None, duration_s=1800, usable=True):
    return Itinerary(
        duration_s=duration_s,
        interchange_stops=interchange_stops if interchange_stops is not None else [],
        leg_modes=leg_modes if leg_modes is not None else ["national-rail"],
        departure_dt=dt.datetime(2026, 9, 1, 12, 0),
        steps=[],
        disruptions=[],
        usable=usable,
    )


def test_1_identical_sequence_under_threshold_matches():
    baseline = _baseline(duration_s=1800)
    candidate = _itinerary(duration_s=1900)
    assert _itinerary_matches(baseline, candidate, threshold_pct=20) is True


def test_2_duration_exactly_at_threshold_boundary_matches():
    baseline = _baseline(duration_s=1000)
    candidate = _itinerary(duration_s=1200)  # exactly +20%
    assert _itinerary_matches(baseline, candidate, threshold_pct=20) is True


def test_3_duration_one_minute_over_threshold_fails():
    baseline = _baseline(duration_s=1000)
    candidate = _itinerary(duration_s=1260)  # +21%, one 60s quantum over
    assert _itinerary_matches(baseline, candidate, threshold_pct=20) is False


def test_4_mode_changed_to_bus_fails():
    baseline = _baseline(leg_modes=["national-rail"])
    candidate = _itinerary(leg_modes=["bus"])
    assert _itinerary_matches(baseline, candidate, threshold_pct=20) is False


def test_5_mode_changed_to_replacement_bus_fails():
    baseline = _baseline(leg_modes=["national-rail"])
    candidate = _itinerary(leg_modes=["replacement-bus"])
    assert _itinerary_matches(baseline, candidate, threshold_pct=20) is False


def test_6_extra_interchange_hop_fails():
    baseline = _baseline(interchange_stops=[], leg_modes=["national-rail"])
    candidate = _itinerary(interchange_stops=["910GCLPHMJC"], leg_modes=["national-rail", "national-rail"])
    assert _itinerary_matches(baseline, candidate, threshold_pct=20) is False


def test_7_fewer_interchange_stops_still_fails():
    baseline = _baseline(interchange_stops=["910GCLPHMJC"], leg_modes=["national-rail", "national-rail"])
    candidate = _itinerary(interchange_stops=[], leg_modes=["national-rail"])
    assert _itinerary_matches(baseline, candidate, threshold_pct=20) is False


def test_8_reordered_interchange_stations_fails():
    baseline = _baseline(interchange_stops=["A", "B"], leg_modes=["national-rail"] * 3)
    candidate = _itinerary(interchange_stops=["B", "A"], leg_modes=["national-rail"] * 3)
    assert _itinerary_matches(baseline, candidate, threshold_pct=20) is False


def test_9_walking_leg_excluded_both_sides_still_matches():
    # Baseline captured without an itemized walk; candidate has the same journey with TfL
    # itemizing the interchange as a walking leg -- both derive the same (empty) interchange
    # list once walking legs are excluded upstream, so this must still match.
    baseline = _baseline(interchange_stops=[], leg_modes=["national-rail"])
    candidate = _itinerary(interchange_stops=[], leg_modes=["national-rail"])
    assert _itinerary_matches(baseline, candidate, threshold_pct=20) is True


def test_10_naptan_id_resolves_correctly():
    from tfl_client import _leg_point_naptan_id
    leg = {"arrivalPoint": {"id": None, "naptanId": "910GCLPHMJC"}}
    assert _leg_point_naptan_id(leg, "arrivalPoint") == "910GCLPHMJC"


def test_11_leg_with_no_ids_marked_unusable():
    from tfl_client import _extract_itinerary
    journey = {
        "duration": 30,
        "startDateTime": "2026-09-01T12:00:00",
        "legs": [
            {"mode": {"id": "national-rail"}, "departurePoint": {}, "arrivalPoint": {}},
            {"mode": {"id": "national-rail"}, "departurePoint": {}, "arrivalPoint": {}},
        ],
    }
    it = _extract_itinerary(journey)
    assert it.usable is False


def test_11b_unusable_itinerary_never_matches():
    baseline = _baseline()
    candidate = _itinerary(usable=False)
    assert _itinerary_matches(baseline, candidate, threshold_pct=20) is False


def test_12_zero_transit_legs_never_matches():
    baseline = _baseline(interchange_stops=[], leg_modes=[])
    candidate = _itinerary(interchange_stops=[], leg_modes=[])
    assert _itinerary_matches(baseline, candidate, threshold_pct=20) is False
