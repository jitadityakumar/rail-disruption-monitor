import pytest
from fastapi.testclient import TestClient

import tfl_client
from conftest import insert_route


@pytest.fixture
def client(db):
    import main
    with TestClient(main.app) as c:
        yield c


def test_25_search_empty_query_no_tfl_call(client, monkeypatch):
    called = []
    monkeypatch.setattr(tfl_client, "_get", lambda url: called.append(url) or {})
    resp = client.get("/api/stations/search?q=")
    assert resp.status_code == 200
    assert resp.json() == []
    assert called == []


def test_26_hub_prefixed_id_rejected_at_model_layer(client):
    payload = {
        "origin": {"id": "HUBWAT", "name": "Waterloo"},
        "destination": {"id": "910GBARNES", "name": "Barnes"},
    }
    resp = client.post("/api/routes", json=payload)
    assert resp.status_code == 422


def test_27_nonexistent_stop_id_400(client, monkeypatch):
    monkeypatch.setattr(tfl_client, "stop_point_exists", lambda stop_id: False)
    payload = {
        "origin": {"id": "910GNOPE", "name": "Nope"},
        "destination": {"id": "910GBARNES", "name": "Barnes"},
    }
    resp = client.post("/api/routes", json=payload)
    assert resp.status_code == 400
    assert "invalid_stations" in resp.json()["detail"]


def test_29_hub_result_expands_to_concrete_rail_children(monkeypatch):
    """Found live: TfL's /StopPoint/Search for "waterloo" returns only the HUBWAT hub group
    (plus unrelated matches), never the concrete "London Waterloo Rail Station" the app's
    routes actually need -- search_stop_points must expand a hub match's children so a
    creatable station is actually reachable from that query."""
    def fake_get(url):
        if "StopPoint/Search" in url:
            return {"matches": [
                {"id": "HUBWAT", "name": "Waterloo", "modes": ["national-rail", "tube", "bus"]},
                {"id": "910GWLOE", "name": "London Waterloo East Rail Station", "modes": ["national-rail"]},
            ]}
        if url.endswith("StopPoint/HUBWAT"):
            return {"children": [
                {"id": "490G000275", "commonName": "Waterloo Station / York Road", "modes": ["bus"]},
                {"id": "910GWATRLMN", "commonName": "London Waterloo Rail Station", "modes": ["national-rail"]},
                {"id": "940GZZLUWLO", "commonName": "Waterloo Underground Station", "modes": ["tube"]},
            ]}
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(tfl_client, "_get", fake_get)
    results = tfl_client.search_stop_points("waterloo")
    ids = [r["id"] for r in results]
    assert "910GWATRLMN" in ids
    assert "940GZZLUWLO" in ids  # tube is in _SEARCH_MODES too
    assert "490G000275" not in ids  # bus child excluded, not in _SEARCH_MODES
    assert "HUBWAT" in ids  # original hub entry still present


def test_28_valid_route_created(client, monkeypatch):
    monkeypatch.setattr(tfl_client, "stop_point_exists", lambda stop_id: True)
    payload = {
        "origin": {"id": "910GBARNES", "name": "Barnes"},
        "destination": {"id": "910GWATRLMN", "name": "London Waterloo"},
    }
    resp = client.post("/api/routes", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Barnes to London Waterloo"
