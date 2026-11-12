# Live Recorder Dashboard — Project Snapshot

## Overview
Web dashboard (FastAPI + static HTML/JS) built on the single-file recorder `tlr.py`
([tiktok-live-lite](https://github.com/yazanbsh/tiktok-live-lite), pinned at **v1.1.0**, unmodified at repo root).
Monitors TikTok users, records their lives, downloads TikTok videos/photos via tikwm, manages the files.
YouTube support was removed completely.

---

## File Tree
```
repo_root/
├── tlr.py                     # recorder (stdlib + ffmpeg), run as a subprocess
├── Dockerfile.web
├── compose.web.yml            # repo example only; live compose is separate
├── entrypoint.web.sh          # exec uvicorn web.server:app
├── .dockerignore
├── .github/workflows/docker-build.yml   # push to main -> ghcr.io/<owner>/tiktok-live-lite:web
└── web/
    ├── server.py              # FastAPI init, routers, logging, startup/shutdown hooks
    ├── requirements-web.txt   # fastapi, uvicorn, httpx, python-multipart
    ├── routers/
    │   ├── shared/
    │   │   ├── config.py      # paths + constants
    │   │   └── files.py       # recordings serve/delete, thumbnails, /api/logs
    │   └── tiktok/
    │       ├── watchlist.py   # monitor loop, tlr subprocess control
    │       └── downloader.py  # tikwm download queue + SSE + import
    └── static/
        ├── index.html
        ├── css/main.css
        └── js/
            ├── tiktok.js      # watchlist, recordings, downloader, import
            └── core.js        # nav, apiFetch, toast, Plyr modal, stats, logs
```

---

## Key Paths (config.py)
```
DATA_DIR        = /data  (env DATA_DIR)
RECORDINGS_DIR  = /data/recordings/{username}/   # TK_<user>_<date>.mp4 from tlr
DOWNLOADS_DIR   = /data/downloads/{username}/    # photos in pics/
WATCHLIST_FILE  = /data/watchlist.json
QUEUE_FILE      = /data/queue.json
APP_LOG_FILE    = /data/logs/app.log             # RotatingFileHandler 5MB x3 + stdout
COOKIES_FILE    = /data/cookies.json             # only passed to users with use_cookies
TLR_PATH        = /app/tlr.py  (env TLR_PATH)
THUMBNAILS_DIR  = /data/thumbnails/{recordings|downloads}/...
```

---

## Docker
- Base `python:3.13-alpine`, uv builder stage installs `web/requirements-web.txt` only.
- Runtime apk: `ffmpeg` (provides ffprobe) and `bash`.
- Non-root uid/gid 1000; volume `/data`; port `WEBUI_PORT` (default 8000).
- Image copies `tlr.py`, `web/`, `entrypoint.web.sh`. No `src/`, `pyproject.toml`, `uv.lock`.
- Compose: `./data:/data`, `stop_grace_period: 120s` (tlr needs time to finalize MP4 on stop).
- CI: GitHub Actions builds/pushes on push to main. Do not merge untested changes.

### Critical notes
- Imports use the `web.routers...` prefix (uvicorn runs `web.server:app` from repo root).
- Script order in index.html: `tiktok.js` then `core.js`.

---

## Live Recording (tlr integration) — `routers/tiktok/watchlist.py`
- One **tlr subprocess per recording**: `python tlr.py -user U -mode manual -output /data/recordings/U [-duration N] [-proxy P] [-cookies /data/cookies.json]`.
  Never passes `-bitrate` (stream copy, no re-encode).
- Live check: `tlr --check` (cached nowhere; skipped while user is recording).
- Own polling loop per automatic user (`interval` minutes). `record` flag = monitor-only when false.
- Exit codes: **0** saved, **1** resolve failed (last `[x]` line -> ⚠ `last_error`), **2** not live, **3** live but no data.
- tlr stdout lines forwarded to app.log as `@user [+]/[!]/[x] ...` (logger `tlr`).
- Stop: SIGTERM to the process (tlr finalizes `.ts` -> MP4); SIGKILL after 110s.
- App shutdown hook (`shutdown_watchlist`) stops all workers/recordings; startup resets stale `recording` status.
- Per-user `use_cookies` (default off); toggled via 🍪 button in the watchlist row or Add form.
- Watchlist entry fields: username, mode (automatic|manual), interval, proxy, duration, use_cookies, record, status, last_seen_live, last_error, added_at.
- Status values: idle, monitoring, recording, error, stopped. `last_error` cleared on success.

### API
| Method | Path | Description |
|---|---|---|
| GET/POST | /api/users | list (with recordings_count) / add |
| DELETE/PATCH | /api/users/{u} | remove / update (mode, interval, proxy, use_cookies, record) |
| POST | /api/users/{u}/record | manual record (needs live) |
| POST | /api/users/{u}/stop | stop active recording |
| GET | /api/users/{u}/status | live check |
| GET | /api/stats | totals, recording count, disk |

---

## TikTok Downloader (`routers/tiktok/downloader.py`)
- Persistent queue `/data/queue.json`; on restart `processing` -> `interrupted`; no auto-resume, worker starts only on user action. SSE snapshot on connect, then updates.
- Video: tikwm `POST /api/video/task/submit` (`{"url": full_url}`) then poll result until `status==2` and `play_url`.
- Photo: `GET https://www.tikwm.com/api/?url={id}&hd=1`; images saved to `username/pics/`, logged as `POSTID_N`.
- Short URLs (vm./vt.tiktok.com) resolved via redirect first.
- Statuses: waiting -> processing -> downloaded / skipped / error / interrupted.
- Files `username_<videoid>.mp4`; log `/data/downloads/{username}/{username}_log.json` (keyed by video id or `POSTID_N`); deleting a file removes its log entry.

| Method | Path |
|---|---|
| POST | /api/tiktok/downloads (enqueue) |
| GET | /api/tiktok/downloads/stream (SSE) |
| POST | /api/tiktok/downloads/resume-queue |
| DELETE | /api/tiktok/downloads/queue (by status list), /queue/{id} |
| POST | /api/tiktok/downloads/queue/{id}/resume |
| GET/DELETE | /api/tiktok/downloads (list / batch delete) |
| GET | /api/tiktok/downloads/{u}/{file} (inline=true to view) |
| POST | /api/tiktok/imports/review, /api/tiktok/imports/file (multipart, one file at a time) |

Import naming: `7330367233635421445_HD.mp4` -> `username_7330367233635421445.mp4`.

---

## Shared files (`routers/shared/files.py`)
- `/api/recordings` list, `/{u}/{file}` serve (inline), batch DELETE, `/{u}/{file}/thumbnail`.
- `/api/tiktok/downloads/{u}/{file}/thumbnail` (checks root dir then `pics/`; images served directly).
- `/api/logs` serves app.log. (`/api/logs/tiktok` removed.)
- Thumbnails: lazy ffmpeg frame at 5s (fallback 1s/0s), 320px wide.

---

## Frontend
Panels: Watchlist, TK Recordings, Downloader, Downloads, Logs (single App Log).
- Per-user collapsible sections (collapsed by default), sort (date/name/size), list/grid toggle, checkboxes + batch bar.
- Plyr modal for playback; filenames link to inline view; images use 🖼 View.
- Add User form: username, mode, interval, proxy, duration, Use cookies, Record when live.

---

## Known Issues / TODO
- Untested end to end after the tlr migration: verify a real recording (ffprobe h264/aac, Plyr play + seek) before merging to main.
- `[x]`/`[!]` markers assumed from tlr output; ⚠ tooltip falls back to a generic message if they differ.
- tlr `-proxy` applies only to its Python HTTP calls, not ffmpeg.
- Crash mid-recording can leave orphaned `.ts` parts; no recovery step yet.
- Peak disk use ~2-3x during finalize.
- No zip download; import supports mp4 only; no per-file progress in downloader.
- Users who relied on cookies must tick Use cookies (default off).
