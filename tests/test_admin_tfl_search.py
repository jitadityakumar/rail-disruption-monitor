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
        "scan_days": [5, 6],
    }
    resp = client.post("/api/routes", json=payload)
    assert resp.status_code == 422


def test_27_nonexistent_stop_id_400(client, monkeypatch):
    monkeypatch.setattr(tfl_client, "stop_point_exists", lambda stop_id: False)
    payload = {
        "origin": {"id": "910GNOPE", "name": "Nope"},
        "destination": {"id": "910GBARNES", "name": "Barnes"},
        "scan_days": [5, 6],
    }
    resp = client.post("/api/routes", json=payload)
    assert resp.status_code == 400
    assert "invalid_stations" in resp.json()["detail"]


def test_28_valid_route_created(client, monkeypatch):
    monkeypatch.setattr(tfl_client, "stop_point_exists", lambda stop_id: True)
    payload = {
        "origin": {"id": "910GBARNES", "name": "Barnes"},
        "destination": {"id": "910GWATRLMN", "name": "London Waterloo"},
        "scan_days": [5, 6],
    }
    resp = client.post("/api/routes", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Barnes to London Waterloo"
