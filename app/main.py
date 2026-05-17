from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import init_db
from scheduler import setup_scheduler, shutdown_scheduler
from stations import load_station_list
from routers import admin, reports, display

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

app.include_router(admin.router)
app.include_router(reports.router)
app.include_router(display.router)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/admin")


@app.get("/api/health")
def health():
    return {"status": "ok"}
