from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import init_db
from scheduler import setup_scheduler, shutdown_scheduler
from stations import load_station_list
from routers import admin, reports, kiosk

templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_station_list()
    setup_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="Rail Disruption Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

app.include_router(admin.router)
app.include_router(reports.router)
app.include_router(kiosk.router)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/admin")


@app.get("/api/health")
def health():
    return {"status": "ok"}
