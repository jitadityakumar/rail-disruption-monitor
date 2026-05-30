from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from database import get_db
from routers.reports import _build_route_data
from shared_templates import templates

router = APIRouter()


@router.get("/kiosk", response_class=HTMLResponse)
def get_kiosk_page(request: Request):
    return templates.TemplateResponse(request, "kiosk.html")


@router.get("/api/kiosk")
def get_kiosk_data():
    db = get_db()
    try:
        return _build_route_data(db, kiosk_only=True)
    finally:
        db.close()
