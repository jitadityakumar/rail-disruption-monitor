def route_display_name(route) -> str:
    return f"{route['origin_name']} to {route['destination_name']}"


def route_direction_labels(route) -> list[dict]:
    return [
        {"key": "outbound", "label": f"{route['origin_name']} → {route['destination_name']}"},
        {"key": "return", "label": f"{route['destination_name']} → {route['origin_name']}"},
    ]
