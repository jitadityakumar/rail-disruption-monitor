import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import tfl_client
from database import get_db
from display import route_display_name, route_via_label
from models import BaselineConfirm, BaselineTrigger, RouteCreate, RouteUpdate
from scanner import confirm_baseline, fetch_baseline_options, scan_all_routes, scan_route
from scheduler import get_next_run, get_schedule_label
from shared_templates import templates

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def get_admin_page(request: Request):
    return templates.TemplateResponse(request, "admin.html", {
        "schedule_label": get_schedule_label(),
        "next_scan": get_next_run(),
    })


@router.get("/api/routes")
def list_routes():
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM routes ORDER BY created_at").fetchall()
        baseline_route_ids = {"outbound": set(), "return": set()}
        outbound_baselines = {}
        for r in db.execute("SELECT * FROM baselines").fetchall():
            if r["direction"] in baseline_route_ids:
                baseline_route_ids[r["direction"]].add(r["route_id"])
            if r["direction"] == "outbound":
                outbound_baselines[r["route_id"]] = r
    finally:
        db.close()
    result = []
    for row in rows:
        d = dict(row)
        d["kiosk_visible"] = bool(d["kiosk_visible"])
        d["has_baseline"] = (
            row["id"] in baseline_route_ids["outbound"] and row["id"] in baseline_route_ids["return"]
        )
        d["display_name"] = route_display_name(row)
        outbound_baseline = outbound_baselines.get(row["id"])
        d["via_label"] = route_via_label(outbound_baseline) if outbound_baseline else None
        result.append(d)
    return result


@router.post("/api/routes", status_code=201)
def create_route(body: RouteCreate):
    invalid = [s.id for s in (body.origin, body.destination) if not tfl_client.stop_point_exists(s.id)]
    if invalid:
        raise HTTPException(status_code=400, detail={"invalid_stations": invalid})

    name = body.name or f"{body.origin.name} to {body.destination.name}"
    db = get_db()
    try:
        if body.kiosk_visible:
            kiosk_count = db.execute("SELECT COUNT(*) FROM routes WHERE kiosk_visible = 1").fetchone()[0]
            if kiosk_count >= 3:
                raise HTTPException(
                    status_code=422,
                    detail="Kiosk already shows 3 routes. Remove a route from kiosk before adding another.",
                )

        cur = db.execute(
            """INSERT INTO routes
               (name, origin_stop_id, origin_name, destination_stop_id, destination_name,
                departure_time, return_time, threshold_pct, kiosk_visible, kiosk_color)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, body.origin.id, body.origin.name, body.destination.id, body.destination.name,
                body.departure_time, body.return_time, body.threshold_pct,
                int(body.kiosk_visible), body.kiosk_color,
            ),
        )
        db.commit()
        route_id = cur.lastrowid
    finally:
        db.close()
    return {"id": route_id, "name": name}


@router.patch("/api/routes/{route_id}")
def update_route(route_id: int, body: RouteUpdate):
    db = get_db()
    try:
        row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Route not found")

        fields = []
        params = []
        if body.name is not None:
            fields.append("name = ?")
            params.append(body.name)
        if body.departure_time is not None:
            fields.append("departure_time = ?")
            params.append(body.departure_time)
        if body.return_time is not None:
            fields.append("return_time = ?")
            params.append(body.return_time)
        if body.threshold_pct is not None:
            fields.append("threshold_pct = ?")
            params.append(body.threshold_pct)
        if body.kiosk_visible is not None:
            if body.kiosk_visible and not row["kiosk_visible"]:
                kiosk_count = db.execute("SELECT COUNT(*) FROM routes WHERE kiosk_visible = 1").fetchone()[0]
                if kiosk_count >= 3:
                    raise HTTPException(
                        status_code=422,
                        detail="Kiosk already shows 3 routes. Remove a route from kiosk before adding another.",
                    )
            fields.append("kiosk_visible = ?")
            params.append(int(body.kiosk_visible))
        if body.kiosk_color is not None:
            fields.append("kiosk_color = ?")
            params.append(body.kiosk_color)

        if fields:
            params.append(route_id)
            db.execute(f"UPDATE routes SET {', '.join(fields)} WHERE id = ?", params)
            db.commit()
    finally:
        db.close()
    return {"ok": True}


@router.delete("/api/routes/{route_id}", status_code=204)
def delete_route(route_id: int):
    db = get_db()
    try:
        row = db.execute("SELECT id FROM routes WHERE id = ?", (route_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Route not found")
        db.execute("UPDATE api_usage_log SET route_id = NULL WHERE route_id = ?", (route_id,))
        db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        db.commit()
    finally:
        db.close()


@router.post("/api/routes/{route_id}/baseline/options")
def baseline_options(route_id: int, body: BaselineTrigger):
    try:
        options = fetch_baseline_options(route_id, body.baseline_date)
        return {"ok": True, "options": options}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/routes/{route_id}/baseline/confirm")
def confirm_baseline_endpoint(route_id: int, body: BaselineConfirm):
    def _to_dict(choice):
        return {
            "duration_s": choice.duration_s,
            "interchange_stops": choice.interchange_stops,
            "leg_modes": choice.leg_modes,
            "steps": choice.steps,
        }

    try:
        confirm_baseline(route_id, body.baseline_date, _to_dict(body.outbound), _to_dict(body.return_))
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/routes/{route_id}/baseline")
def get_baseline(route_id: int):
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM baselines WHERE route_id = ?", (route_id,)).fetchall()
    finally:
        db.close()
    if not rows:
        raise HTTPException(status_code=404, detail="No baseline for this route")

    result = {"baseline_date": None, "captured_at": None, "outbound": None, "return": None}
    for row in rows:
        result["baseline_date"] = row["baseline_date"]
        result["captured_at"] = row["captured_at"]
        result[row["direction"]] = {
            "duration_s": row["duration_s"],
            "interchange_stops": json.loads(row["interchange_stops"]),
            "leg_modes": json.loads(row["leg_modes"]),
            "steps": json.loads(row["steps"]),
        }
    return result


@router.post("/api/routes/{route_id}/scan")
def trigger_scan(route_id: int):
    try:
        result = scan_route(route_id)
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/scan-all")
async def trigger_scan_all():
    loop = asyncio.get_running_loop()
    asyncio.ensure_future(loop.run_in_executor(None, scan_all_routes))
    return {"ok": True, "message": "Scan started in background"}


@router.get("/api/stations/search")
def search_stations(q: str = ""):
    return tfl_client.search_stop_points(q)
