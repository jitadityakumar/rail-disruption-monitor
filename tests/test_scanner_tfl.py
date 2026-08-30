import datetime as dt
import json

import pytest

import scanner
import tfl_client
from tfl_client import Itinerary, TflResult
from conftest import insert_both_baselines, insert_route


def _it(duration_s=1800, interchange_stops=None, leg_modes=None, departure_dt=None,
        usable=True, steps=None, disruptions=None):
    return Itinerary(
        duration_s=duration_s,
        interchange_stops=interchange_stops if interchange_stops is not None else [],
        leg_modes=leg_modes if leg_modes is not None else ["national-rail"],
        departure_dt=departure_dt or dt.datetime(2026, 9, 1, 12, 0),
        steps=steps if steps is not None else [{"mode": "national-rail"}],
        disruptions=disruptions if disruptions is not None else [],
        usable=usable,
    )


def _route_row(db, threshold_pct=20):
    route_id = insert_route(db, threshold_pct=threshold_pct)
    insert_both_baselines(db, route_id, duration_s=1800, interchange_stops=[], leg_modes=["national-rail"])
    conn = db.get_db()
    row = conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    baseline = conn.execute(
        "SELECT * FROM baselines WHERE route_id = ? AND direction = 'outbound'", (route_id,)
    ).fetchone()
    conn.close()
    return route_id, row, baseline


def test_13_first_page_match_one_call(db, monkeypatch):
    route_id, row, baseline = _route_row(db)
    calls = []

    def fake_fetch(origin, dest, query_dt, route_id_arg, purpose):
        calls.append(query_dt)
        return TflResult(ok=True, itineraries=[_it(duration_s=1800)])

    monkeypatch.setattr(tfl_client, "fetch_journeys_at", fake_fetch)
    result = scanner._scan_direction(row, baseline, "2026-09-01", "outbound")
    assert result["status"] == "NORMAL"
    assert len(calls) == 1


def test_14_match_on_second_page_two_calls(db, monkeypatch):
    route_id, row, baseline = _route_row(db)
    responses = [
        TflResult(ok=True, itineraries=[_it(duration_s=9999, leg_modes=["bus"], departure_dt=dt.datetime(2026, 9, 1, 12, 0))]),
        TflResult(ok=True, itineraries=[_it(duration_s=1800, departure_dt=dt.datetime(2026, 9, 1, 12, 30))]),
    ]

    def fake_fetch(origin, dest, query_dt, route_id_arg, purpose):
        return responses.pop(0)

    monkeypatch.setattr(tfl_client, "fetch_journeys_at", fake_fetch)
    result = scanner._scan_direction(row, baseline, "2026-09-01", "outbound")
    assert result["status"] == "NORMAL"
    assert result["calls_made"] == 2


def test_15_no_match_window_exhausts_disrupted(db, monkeypatch):
    route_id, row, baseline = _route_row(db)

    def fake_fetch(origin, dest, query_dt, route_id_arg, purpose):
        # Each call's "next journey" departs 40 minutes after the query time, so the 120-minute
        # window is fully walked in 3 calls (well under the 8-call cap).
        return TflResult(ok=True, itineraries=[_it(duration_s=9999, leg_modes=["bus"], departure_dt=query_dt + dt.timedelta(minutes=40))])

    monkeypatch.setattr(tfl_client, "fetch_journeys_at", fake_fetch)
    result = scanner._scan_direction(row, baseline, "2026-09-01", "outbound")
    assert result["status"] == "DISRUPTED"
    assert result["window_fully_walked"] is True
    assert result["calls_made"] < scanner.TFL_MAX_CALLS_PER_DATE_DIRECTION


def test_16_call_cap_reached_before_window_ends_is_unknown(db, monkeypatch):
    route_id, row, baseline = _route_row(db)
    base_dt = dt.datetime(2026, 9, 1, 12, 0)

    def fake_fetch(origin, dest, query_dt, route_id_arg, purpose):
        # Always return a non-matching journey departing just 1 minute later, so the window
        # (120 min) never naturally exhausts before the 8-call cap does.
        return TflResult(ok=True, itineraries=[_it(duration_s=9999, leg_modes=["bus"], departure_dt=query_dt + dt.timedelta(minutes=1))])

    monkeypatch.setattr(tfl_client, "fetch_journeys_at", fake_fetch)
    result = scanner._scan_direction(row, baseline, "2026-09-01", "outbound")
    assert result["status"] == "UNKNOWN"
    assert result["calls_made"] == scanner.TFL_MAX_CALLS_PER_DATE_DIRECTION
    assert result["window_fully_walked"] is False


def test_17_api_error_on_first_call_unknown(db, monkeypatch):
    route_id, row, baseline = _route_row(db)

    def fake_fetch(origin, dest, query_dt, route_id_arg, purpose):
        return TflResult(ok=False, error="network_error")

    monkeypatch.setattr(tfl_client, "fetch_journeys_at", fake_fetch)
    result = scanner._scan_direction(row, baseline, "2026-09-01", "outbound")
    assert result["status"] == "UNKNOWN"
    assert result["calls_made"] == 1
    assert result["window_fully_walked"] is False


def test_18_api_error_on_later_call_unknown_not_disrupted(db, monkeypatch):
    route_id, row, baseline = _route_row(db)
    responses = [
        TflResult(ok=True, itineraries=[_it(duration_s=9999, leg_modes=["bus"], departure_dt=dt.datetime(2026, 9, 1, 12, 0))]),
        TflResult(ok=False, error="timeout"),
    ]

    def fake_fetch(origin, dest, query_dt, route_id_arg, purpose):
        return responses.pop(0)

    monkeypatch.setattr(tfl_client, "fetch_journeys_at", fake_fetch)
    result = scanner._scan_direction(row, baseline, "2026-09-01", "outbound")
    assert result["status"] == "UNKNOWN"
    assert result["calls_made"] == 2


def test_19_information_disruption_filtered_on_normal_day(db, monkeypatch):
    route_id, row, baseline = _route_row(db)

    def fake_fetch(origin, dest, query_dt, route_id_arg, purpose):
        return TflResult(ok=True, itineraries=[_it(duration_s=1800, disruptions=[{"category": "Information", "description": "noise"}])])

    monkeypatch.setattr(tfl_client, "fetch_journeys_at", fake_fetch)
    result = scanner._scan_direction(row, baseline, "2026-09-01", "outbound")
    assert result["status"] == "NORMAL"


def test_20_alternate_steps_is_fastest_across_whole_window(db, monkeypatch):
    route_id, row, baseline = _route_row(db)
    responses = [
        TflResult(ok=True, itineraries=[_it(duration_s=5000, leg_modes=["bus"], steps=[{"mode": "bus", "summary": "slow"}], departure_dt=dt.datetime(2026, 9, 1, 12, 0))]),
        TflResult(ok=True, itineraries=[_it(duration_s=3000, leg_modes=["bus"], steps=[{"mode": "bus", "summary": "fast"}], departure_dt=dt.datetime(2026, 9, 1, 14, 0))]),
    ]

    def fake_fetch(origin, dest, query_dt, route_id_arg, purpose):
        return responses.pop(0) if responses else TflResult(ok=True, itineraries=[])

    monkeypatch.setattr(tfl_client, "fetch_journeys_at", fake_fetch)
    result = scanner._scan_direction(row, baseline, "2026-09-01", "outbound")
    assert result["status"] == "DISRUPTED"
    assert result["alternate_steps"][0]["summary"] == "fast"


def test_21_missing_baseline_raises(db):
    route_id = insert_route(db)
    with pytest.raises(ValueError):
        scanner.scan_route(route_id)


def test_22_baseline_station_mismatch_is_unknown(db):
    route_id = insert_route(db, origin_stop_id="910GBARNES", destination_stop_id="910GWATRLMN")
    insert_both_baselines(db, route_id, origin_stop_id="910GOTHER", destination_stop_id="910GWATRLMN")
    conn = db.get_db()
    row = conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    baseline = conn.execute(
        "SELECT * FROM baselines WHERE route_id = ? AND direction = 'outbound'", (route_id,)
    ).fetchone()
    conn.close()
    result = scanner._scan_direction(row, baseline, "2026-09-01", "outbound")
    assert result["status"] == "UNKNOWN"
    assert "different stations" in result["reasons"][0]


def test_23_404_no_data_never_disrupted(db, monkeypatch):
    route_id, row, baseline = _route_row(db)

    def fake_fetch(origin, dest, query_dt, route_id_arg, purpose):
        return TflResult(ok=True, itineraries=[], no_data=True)

    monkeypatch.setattr(tfl_client, "fetch_journeys_at", fake_fetch)
    result = scanner._scan_direction(row, baseline, "2026-09-01", "outbound")
    assert result["status"] == "UNKNOWN"
    assert result["status"] != "DISRUPTED"


def test_24_ambiguous_hub_ok_false(monkeypatch):
    def fake_get(url):
        return {"$type": "Some.Type"}  # no "journeys" key -> ambiguous shape

    monkeypatch.setattr(tfl_client, "_get", fake_get)
    result = tfl_client.fetch_journeys("HUBWAT", "910GBARNES", "2026-09-01", "12:00")
    assert result.ok is False
    assert result.error == "ambiguous_stop_point"


def test_32_scan_direction_uses_per_direction_time(db, monkeypatch):
    """departure_time/return_time replaced the old shared SCAN_SLOT constant (issue #24
    follow-up) -- outbound must query at the route's departure_time, return at its
    return_time, not the same slot for both."""
    route_id = insert_route(db, departure_time="07:15", return_time="19:45")
    insert_both_baselines(db, route_id, duration_s=1800, interchange_stops=[], leg_modes=["national-rail"])
    conn = db.get_db()
    row = conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    outbound_baseline = conn.execute(
        "SELECT * FROM baselines WHERE route_id = ? AND direction = 'outbound'", (route_id,)
    ).fetchone()
    return_baseline = conn.execute(
        "SELECT * FROM baselines WHERE route_id = ? AND direction = 'return'", (route_id,)
    ).fetchone()
    conn.close()

    seen_query_dts = []

    def fake_fetch(origin, dest, query_dt, route_id_arg, purpose):
        seen_query_dts.append(query_dt)
        return TflResult(ok=True, itineraries=[_it(departure_dt=query_dt)])

    monkeypatch.setattr(tfl_client, "fetch_journeys_at", fake_fetch)
    scanner._scan_direction(row, outbound_baseline, "2026-09-01", "outbound")
    scanner._scan_direction(row, return_baseline, "2026-09-01", "return")

    assert seen_query_dts[0] == dt.datetime(2026, 9, 1, 7, 15)
    assert seen_query_dts[1] == dt.datetime(2026, 9, 1, 19, 45)


def test_33_scan_window_end_date_current_plus_next_month():
    """scan_window_end_date replaced the old per-route lookahead_weeks knob with a fixed
    current-month-plus-next-month window (issue #24 follow-up)."""
    assert scanner.scan_window_end_date(dt.date(2026, 8, 30)) == dt.date(2026, 9, 30)
    assert scanner.scan_window_end_date(dt.date(2026, 1, 15)) == dt.date(2026, 2, 28)
    assert scanner.scan_window_end_date(dt.date(2026, 11, 5)) == dt.date(2026, 12, 31)
    assert scanner.scan_window_end_date(dt.date(2026, 12, 5)) == dt.date(2027, 1, 31)


def test_34_target_dates_include_every_weekday(db):
    """scan_days removed (issue #24 follow-up) -- every day in the window is scanned, not
    just a configured subset of weekdays."""
    dates = scanner._target_dates()
    weekdays_present = {d.weekday() for d in dates}
    assert weekdays_present == set(range(7))
