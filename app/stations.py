"""
CRS code utilities: name lookup from stations.json and lat/lon from RailData API.

RailData XML structure (confirmed from BNS test call):
  Root element: <StationV4.0 xmlns="http://nationalrail.co.uk/xml/station" ...>
  Direct children (in default namespace):
    <Latitude>51.46706806</Latitude>
    <Longitude>-0.240724454</Longitude>
"""

import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from database import get_db

_STATION_LIST: dict[str, str] = {}
_STATION_NS = "http://nationalrail.co.uk/xml/station"
_RAILDATA_KEY = os.environ.get("RAILDATA_STATIONS_API_KEY", "")
_RAILDATA_URL = "https://api1.raildata.org.uk/1010-knowlegebase-stations-xml-feed1_1/4.0/station-{crs}.xml"


def load_station_list() -> None:
    global _STATION_LIST
    path = Path(__file__).parent / "stations.json"
    with open(path) as f:
        data = json.load(f)
    _STATION_LIST = {s["crs"]: s["Value"] for s in data["StationList"]}


def validate_crs(crs: str) -> bool:
    return crs.upper() in _STATION_LIST


def get_station_name(crs: str) -> str | None:
    return _STATION_LIST.get(crs.upper())


def route_leg_labels(row) -> list[dict]:
    o = get_station_name(row["origin_crs"]) or row["origin_crs"]
    d = get_station_name(row["destination_crs"]) or row["destination_crs"]
    c = (get_station_name(row["change_crs"]) or row["change_crs"]) if row["change_crs"] else None
    if c:
        return [
            {"key": "outbound_1", "label": f"{o} → {c}"},
            {"key": "outbound_2", "label": f"{c} → {d}"},
            {"key": "return_1",   "label": f"{d} → {c}"},
            {"key": "return_2",   "label": f"{c} → {o}"},
        ]
    return [
        {"key": "outbound_1", "label": f"{o} → {d}"},
        {"key": "return_1",   "label": f"{d} → {o}"},
    ]


def leg_label(row, direction: str, leg: int) -> str:
    key = f"{direction}_{leg}"
    match = next((l["label"] for l in route_leg_labels(row) if l["key"] == key), None)
    return match or f"{direction} leg {leg}"


def route_display_name(origin_crs: str, destination_crs: str, change_crs: str | None = None) -> str:
    o = get_station_name(origin_crs) or origin_crs
    d = get_station_name(destination_crs) or destination_crs
    if change_crs:
        c = get_station_name(change_crs) or change_crs
        return f"{o} to {d} via {c}"
    return f"{o} to {d}"


def get_coords(crs: str) -> tuple[float, float]:
    crs = crs.upper()
    db = get_db()
    try:
        row = db.execute(
            "SELECT latitude, longitude FROM station_coords WHERE crs = ?", (crs,)
        ).fetchone()
        if row:
            return row["latitude"], row["longitude"]

        url = _RAILDATA_URL.format(crs=crs)
        # Empty User-Agent required — API blocks Python-urllib's default agent
        req = urllib.request.Request(url, headers={"x-apikey": _RAILDATA_KEY, "User-Agent": ""})
        import gzip
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            raw = gzip.decompress(body) if resp.info().get("Content-Encoding") == "gzip" else body
        root = ET.fromstring(raw)
        ns = {"ns": _STATION_NS}
        lat_el = root.find("ns:Latitude", ns)
        lon_el = root.find("ns:Longitude", ns)
        if lat_el is None or lon_el is None:
            raise ValueError(f"Lat/lon not found in RailData response for {crs}")
        lat = float(lat_el.text)
        lon = float(lon_el.text)
        db.execute(
            "INSERT OR REPLACE INTO station_coords (crs, latitude, longitude) VALUES (?, ?, ?)",
            (crs, lat, lon),
        )
        db.commit()
        return lat, lon
    finally:
        db.close()
