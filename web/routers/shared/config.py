"""
Shared configuration — paths, constants.
All routers import from here instead of redeclaring.
"""

import os
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent.parent  # web/
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR.parent / "data")))
STATIC_DIR = BASE_DIR / "static"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
RECORDINGS_DIR = DATA_DIR / "recordings"
DOWNLOADS_DIR = DATA_DIR / "downloads"
QUEUE_FILE = DATA_DIR / "queue.json"
APP_LOG_FILE = DATA_DIR / "logs" / "app.log"

# ── tlr (live recorder, run as a subprocess) ──────────────────────────────────
TLR_PATH = Path(os.environ.get("TLR_PATH", str(BASE_DIR.parent / "tlr.py")))
COOKIES_FILE = DATA_DIR / "cookies.json"  # only passed to users with use_cookies

# tikwm endpoints
TIKWM_SUBMIT = "https://www.tikwm.com/api/video/task/submit"
TIKWM_RESULT = "https://www.tikwm.com/api/video/task/result"

# ── thumbnail paths ──────────────────────────────────────────────────────────
THUMBNAILS_DIR = DATA_DIR / "thumbnails"
THUMB_RECORDINGS_DIR = THUMBNAILS_DIR / "recordings"
THUMB_DOWNLOADS_DIR = THUMBNAILS_DIR / "downloads"

# ensure dirs exist
for _d in (
    RECORDINGS_DIR,
    DOWNLOADS_DIR,
    THUMB_RECORDINGS_DIR,
    THUMB_DOWNLOADS_DIR,
    DATA_DIR / "logs",
):
    _d.mkdir(parents=True, exist_ok=True)
