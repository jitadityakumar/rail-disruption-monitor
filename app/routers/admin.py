import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from database import get_db
from models import BaselineConfirm, BaselineTrigger, RouteCreate, RouteUpdate
from scanner import confirm_baseline, fetch_baseline_options, scan_all_routes, scan_route
from scheduler import get_next_run
from shared_templates import templates
from stations import get_station_name, route_display_name, route_leg_labels, validate_crs

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def get_admin_page(request: Request):
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "next_scan": get_next_run(),
    })


@router.get("/api/routes")
def list_routes():
    db = get_db()
    rows = db.execute("SELECT * FROM routes ORDER BY created_at").fetchall()
    baseline_route_ids = {
        r["route_id"]
        for r in db.execute("SELECT route_id FROM baselines").fetchall()
    }
    db.close()
    result = []
    for row in rows:
        d = dict(row)
        d["scan_days"] = [int(x) for x in d["scan_days"].split(",")]
        d["kiosk_visible"] = bool(d["kiosk_visible"])
        d["has_baseline"] = row["id"] in baseline_route_ids
        d["has_change"] = bool(row["change_crs"])
        d["leg_labels"] = route_leg_labels(row)
        d["display_name"] = route_display_name(row["origin_crs"], row["destination_crs"], row["change_crs"])
        result.append(d)
    return result


@router.post("/api/routes", status_code=201)
def create_route(body: RouteCreate):
    crses = [body.origin_crs]
    if body.change_crs:
        crses.append(body.change_crs)
    crses.append(body.destination_crs)
    invalid = [crs for crs in crses if not validate_crs(crs)]
    if invalid:
        raise HTTPException(status_code=400, detail={"invalid_crs": invalid})

    name = body.name or route_display_name(body.origin_crs, body.destination_crs, body.change_crs)
    db = get_db()

    if body.kiosk_visible:
        kiosk_count = db.execute(
            "SELECT COUNT(*) FROM routes WHERE kiosk_visible = 1"
        ).fetchone()[0]
        if kiosk_count >= 3:
            db.close()
            raise HTTPException(
                status_code=422,
                detail="Kiosk already shows 3 routes. Remove a route from kiosk before adding another.",
            )

    cur = db.execute(
        """INSERT INTO routes
           (name, origin_crs, change_crs, destination_crs, scan_days, lookahead_weeks, threshold_pct, kiosk_visible)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name,
            body.origin_crs,
            body.change_crs,
            body.destination_crs,
            ",".join(str(d) for d in body.scan_days),
            body.lookahead_weeks,
            body.threshold_pct,
            int(body.kiosk_visible),
        ),
    )
    db.commit()
    route_id = cur.lastrowid
    db.close()
    return {"id": route_id, "name": name}


@router.patch("/api/routes/{route_id}")
def update_route(route_id: int, body: RouteUpdate):
    db = get_db()
    row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if not row:
        db.close()
        raise HTTPException(status_code=404, detail="Route not found")

    fields = []
    params = []
    if body.name is not None:
        fields.append("name = ?")
        params.append(body.name)
    if body.scan_days is not None:
        fields.append("scan_days = ?")
        params.append(",".join(str(d) for d in body.scan_days))
    if body.lookahead_weeks is not None:
        fields.append("lookahead_weeks = ?")
        params.append(body.lookahead_weeks)
    if body.threshold_pct is not None:
        fields.append("threshold_pct = ?")
        params.append(body.threshold_pct)
    if body.kiosk_visible is not None:
        if body.kiosk_visible and not row["kiosk_visible"]:
            kiosk_count = db.execute(
                "SELECT COUNT(*) FROM routes WHERE kiosk_visible = 1"
            ).fetchone()[0]
            if kiosk_count >= 3:
                db.close()
                raise HTTPException(
                    status_code=422,
                    detail="Kiosk already shows 3 routes. Remove a route from kiosk before adding another.",
                )
        fields.append("kiosk_visible = ?")
        params.append(int(body.kiosk_visible))

    if fields:
        params.append(route_id)
        db.execute(f"UPDATE routes SET {', '.join(fields)} WHERE id = ?", params)
        db.commit()
    db.close()
    return {"ok": True}


@router.delete("/api/routes/{route_id}", status_code=204)
def delete_route(route_id: int):
    db = get_db()
    row = db.execute("SELECT id FROM routes WHERE id = ?", (route_id,)).fetchone()
    if not row:
        db.close()
        raise HTTPException(status_code=404, detail="Route not found")
    db.execute("UPDATE api_usage_log SET route_id = NULL WHERE route_id = ?", (route_id,))
    db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
    db.commit()
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
    def _to_dict(sel):
        if sel is None:
            return None
        return {"duration_s": sel.duration_s, "steps": sel.steps, "dep_stop": sel.dep_stop, "arr_stop": sel.arr_stop}

    try:
        confirm_baseline(route_id, body.baseline_date, {
            "outbound_leg1": _to_dict(body.outbound_leg1),
            "outbound_leg2": _to_dict(body.outbound_leg2),
            "return_leg1":   _to_dict(body.return_leg1),
            "return_leg2":   _to_dict(body.return_leg2),
        })
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/routes/{route_id}/baseline")
def get_baseline(route_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM baselines WHERE route_id = ?", (route_id,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(status_code=404, detail="No baseline for this route")

    def _leg(dur_col, steps_col, dep_col, arr_col):
        if not row[arr_col]:
            return None
        return {
            "duration_s": row[dur_col],
            "dep_stop": row[dep_col],
            "arr_stop": row[arr_col],
            "steps": json.loads(row[steps_col] or "[]"),
        }

    return {
        "baseline_date": row["baseline_date"],
        "captured_at": row["captured_at"],
        "outbound_leg1": _leg("outbound_leg1_duration_s", "outbound_leg1_steps", "outbound_leg1_dep_stop", "outbound_leg1_arr_stop"),
        "outbound_leg2": _leg("outbound_leg2_duration_s", "outbound_leg2_steps", "outbound_leg2_dep_stop", "outbound_leg2_arr_stop"),
        "return_leg1":   _leg("return_leg1_duration_s",   "return_leg1_steps",   "return_leg1_dep_stop",   "return_leg1_arr_stop"),
        "return_leg2":   _leg("return_leg2_duration_s",   "return_leg2_steps",   "return_leg2_dep_stop",   "return_leg2_arr_stop"),
    }


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
    from stations import _STATION_LIST
    q = q.upper().strip()
    if not q:
        return []
    matches = [
        {"crs": crs, "name": name}
        for crs, name in _STATION_LIST.items()
        if q in crs or q in name.upper()
    ]
    return matches[:20]


@router.get("/api/stations/{crs}")
def get_station(crs: str):
    from stations import get_coords
    crs = crs.upper()
    if not validate_crs(crs):
        raise HTTPException(status_code=404, detail="Station not found")
    try:
        lat, lon = get_coords(crs)
        return {"crs": crs, "name": get_station_name(crs), "latitude": lat, "longitude": lon}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
