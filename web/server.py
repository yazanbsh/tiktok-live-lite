"""
TikTok Live Recorder — Web UI Backend
Place this file at:  <repo-root>/web/server.py
Run with:  uvicorn web.server:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
import logging.handlers
from pathlib import Path
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from web.routers.shared.config import STATIC_DIR
from web.routers.shared.files import router as files_router
from web.routers.tiktok.watchlist import (
    router as tiktok_watchlist_router,
    startup_watchlist,
    shutdown_watchlist,
)
from web.routers.tiktok.downloader import router as tiktok_downloader_router

# ── Logging setup ────────────────────────────────────────────────────────────
_DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
_LOG_FILE = _DATA_DIR / "logs" / "app.log"
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
        logging.StreamHandler(),  # also keep stdout for docker logs
    ],
)

app = FastAPI(title="TikTok Live Recorder", version="2.0.0")

# ── Include routers ───────────────────────────────────────────────────────────
app.include_router(files_router)
app.include_router(tiktok_watchlist_router)
app.include_router(tiktok_downloader_router)


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    startup_watchlist()


@app.on_event("shutdown")
def shutdown():
    # SIGTERM tlr subprocesses so recordings get finalised to MP4
    shutdown_watchlist()


# ── Static files + frontend ───────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
