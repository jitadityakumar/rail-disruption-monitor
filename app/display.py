import json


def route_display_name(route) -> str:
    return f"{route['origin_name']} to {route['destination_name']}"


def route_via_label(baseline) -> str:
    """"Direct" or "via X, Y" derived from an outbound baseline's interchange_stops,
    resolved to station names via that same baseline's steps."""
    interchange_ids = json.loads(baseline["interchange_stops"])
    if not interchange_ids:
        return "Direct"
    steps = json.loads(baseline["steps"])
    names_by_id = {s["arr_id"]: s["arr_name"] for s in steps if s.get("arr_id")}
    names = [names_by_id[stop_id] for stop_id in interchange_ids if stop_id in names_by_id]
    return f"via {', '.join(names)}" if names else "Direct"


def route_direction_labels(route) -> list[dict]:
    return [
        {"key": "outbound", "label": f"{route['origin_name']} → {route['destination_name']}"},
        {"key": "return", "label": f"{route['destination_name']} → {route['origin_name']}"},
    ]
