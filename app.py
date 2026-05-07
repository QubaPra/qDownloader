from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.config import ROOT
from core.state import startup_worker

# Import routers
from routers.common import router as common_router
from routers.youtube import router as youtube_router, run_download as yt_run_download
from routers.twitch import router as twitch_router

app = FastAPI()

@app.on_event("startup")
async def on_startup():
    await startup_worker(yt_run_download)

app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

jinja_env = Environment(
    loader=FileSystemLoader(str(ROOT / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)

@app.get("/", response_class=HTMLResponse)
async def index():
    template = jinja_env.get_template("index.html")
    return template.render()

# Include routers
app.include_router(common_router)
app.include_router(youtube_router)
app.include_router(twitch_router)
