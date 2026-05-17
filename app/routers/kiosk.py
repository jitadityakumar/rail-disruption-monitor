from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import get_db
from routers.reports import _build_route_data

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/kiosk", response_class=HTMLResponse)
def get_kiosk_page(request: Request):
    return templates.TemplateResponse("kiosk.html", {"request": request})


@router.get("/api/kiosk")
def get_kiosk_data():
    db = get_db()
    try:
        return _build_route_data(db, kiosk_only=True)
    finally:
        db.close()
