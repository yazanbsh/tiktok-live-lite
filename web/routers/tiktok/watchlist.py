"""
TikTok live watchlist — add/remove/update users, manual record, live status, stats.

Recording is done by tlr.py, run as one subprocess per recording.
Exit codes: 0 saved, 1 resolve failed, 2 not live, 3 live but no data.
"""

import json
import logging
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..shared.config import WATCHLIST_FILE, RECORDINGS_DIR, TLR_PATH, COOKIES_FILE

router = APIRouter()
log = logging.getLogger("watchlist")
tlr_log = logging.getLogger("tlr")

# ── State ─────────────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_workers: dict[str, threading.Thread] = {}
_stop_events: dict[str, threading.Event] = {}
_rec_stop_events: dict[str, threading.Event] = {}
_procs: dict[str, subprocess.Popen] = {}

CHECK_TIMEOUT = 90
STOP_GRACE = 110  # seconds to wait for tlr to finish remuxing after SIGTERM


def _load_watchlist() -> dict:
    if WATCHLIST_FILE.exists():
        try:
            return json.loads(WATCHLIST_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_watchlist(data: dict):
    WATCHLIST_FILE.write_text(json.dumps(data, indent=2))


watchlist: dict = _load_watchlist()


# ── Models ────────────────────────────────────────────────────────────────────


class AddUserRequest(BaseModel):
    username: str
    mode: str = "automatic"
    interval: int = 5
    proxy: Optional[str] = None
    duration: Optional[int] = None
    use_cookies: bool = False
    record: bool = True


class UpdateUserRequest(BaseModel):
    mode: Optional[str] = None
    interval: Optional[int] = None
    proxy: Optional[str] = None
    use_cookies: Optional[bool] = None
    record: Optional[bool] = None


# ── tlr helpers ───────────────────────────────────────────────────────────────


def _common_args(username: str, entry: dict) -> list[str]:
    args = [sys.executable, str(TLR_PATH), "-user", username]
    if entry.get("proxy"):
        args += ["-proxy", entry["proxy"]]
    if entry.get("use_cookies"):
        if COOKIES_FILE.exists():
            args += ["-cookies", str(COOKIES_FILE)]
        else:
            log.warning("@%s: use_cookies is on but %s does not exist", username, COOKIES_FILE)
    return args


def _last_error(lines: list[str], default: str) -> str:
    for line in reversed(lines):
        if "[x]" in line:
            return line.split("[x]", 1)[1].strip() or default
    return default


def _forward(username: str, line: str):
    if "[x]" in line:
        tlr_log.error("@%s %s", username, line)
    elif "[!]" in line:
        tlr_log.warning("@%s %s", username, line)
    else:
        tlr_log.info("@%s %s", username, line)


def _check_live(username: str, entry: dict) -> tuple[int, str]:
    """Run `tlr --check`. Returns (exit_code, error_text)."""
    try:
        p = subprocess.run(
            _common_args(username, entry) + ["--check"],
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 1, "live check timed out"
    lines = [l.strip() for l in (p.stdout or "").splitlines() if l.strip()]
    return p.returncode, _last_error(lines, "live check failed") if p.returncode == 1 else ""


def _run_recording(username: str, entry: dict, events: list[threading.Event]) -> tuple[int, str]:
    """
    Run one tlr recording (manual mode, single session) and block until it ends.
    If any event in `events` is set, SIGTERM is sent so tlr finalises the MP4.
    """
    cmd = _common_args(username, entry) + [
        "-mode", "manual",
        "-output", str(RECORDINGS_DIR / username),
    ]
    if entry.get("duration"):
        cmd += ["-duration", str(int(entry["duration"]))]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    _procs[username] = proc
    lines: list[str] = []

    def _reader():
        for raw in proc.stdout:
            line = raw.strip()
            if line:
                lines.append(line)
                if len(lines) > 200:
                    del lines[:100]
                _forward(username, line)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    terminated_at = None
    try:
        while proc.poll() is None:
            if terminated_at is None and any(e.is_set() for e in events):
                proc.send_signal(signal.SIGTERM)
                terminated_at = time.time()
            elif terminated_at and time.time() - terminated_at > STOP_GRACE:
                log.error("@%s: tlr did not exit after SIGTERM, killing", username)
                proc.kill()
            time.sleep(1)
    finally:
        t.join(timeout=5)
        _procs.pop(username, None)

    return proc.returncode, _last_error(lines, "recording failed")


def _set(username: str, **fields):
    with _state_lock:
        if username in watchlist:
            watchlist[username].update(fields)
            _save_watchlist(watchlist)


def _apply_result(username: str, code: int, err: str, idle_status: str):
    """Map a tlr exit code onto the watchlist entry."""
    if code == 0:
        _set(username, status=idle_status, last_error=None)
    elif code == 2:
        _set(username, status=idle_status, last_error=None)
    elif code == 3:
        _set(username, status=idle_status, last_error="Live, but no stream data was received")
    else:
        _set(username, status="error", last_error=err)


def _count_recordings(username: str) -> int:
    user_dir = RECORDINGS_DIR / username
    if not user_dir.exists():
        return 0
    return len(list(user_dir.glob("*.mp4")))


# ── Worker ────────────────────────────────────────────────────────────────────


def _recording_worker(username: str, stop_event: threading.Event):
    while not stop_event.is_set():
        with _state_lock:
            entry = dict(watchlist.get(username, {}))
        if not entry:
            break
        interval = entry.get("interval", 5)

        code, err = _check_live(username, entry)
        if stop_event.is_set():
            break

        if code == 0:
            _set(username, last_seen_live=datetime.utcnow().isoformat(), last_error=None)
            if entry.get("record", True):
                _set(username, status="recording")
                rec_stop = threading.Event()
                _rec_stop_events[username] = rec_stop
                rcode, rerr = _run_recording(username, entry, [stop_event, rec_stop])
                _apply_result(username, rcode, rerr, "monitoring")
            else:
                _set(username, status="monitoring", last_error=None)
        elif code == 2:
            _set(username, status="monitoring", last_error=None)
        else:
            _set(username, status="error", last_error=err)

        stop_event.wait(timeout=interval * 60)

    _set(username, status="stopped")


def _start_worker(username: str):
    if username in _workers and _workers[username].is_alive():
        return
    stop_event = threading.Event()
    _stop_events[username] = stop_event
    t = threading.Thread(
        target=_recording_worker,
        args=(username, stop_event),
        daemon=True,
        name=f"worker-{username}",
    )
    t.start()
    _workers[username] = t


def _stop_worker(username: str):
    if username in _rec_stop_events:
        _rec_stop_events[username].set()
    if username in _stop_events:
        _stop_events[username].set()
    if username in _workers:
        _workers[username].join(timeout=10)


# ── Startup / shutdown ────────────────────────────────────────────────────────


def startup_watchlist():
    """Called from server.py on app startup."""
    for username, entry in watchlist.items():
        # a restart always kills any previous recording
        if entry.get("status") == "recording":
            entry["status"] = "idle"
        if entry.get("mode", "automatic") == "automatic":
            entry["status"] = "monitoring"
            _start_worker(username)
    _save_watchlist(watchlist)


def shutdown_watchlist():
    """Called from server.py on app shutdown: let tlr finalise its MP4s."""
    for ev in list(_stop_events.values()) + list(_rec_stop_events.values()):
        ev.set()
    deadline = time.time() + STOP_GRACE + 5
    for t in list(_workers.values()):
        t.join(timeout=max(0.1, deadline - time.time()))


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/api/users")
def list_users():
    with _state_lock:
        users = [dict(e) for e in watchlist.values()]
    for u in users:
        u["recordings_count"] = _count_recordings(u["username"])
    return users


@router.post("/api/users", status_code=201)
def add_user(req: AddUserRequest):
    username = req.username.lstrip("@").strip()
    if not username:
        raise HTTPException(400, "Username cannot be empty")
    with _state_lock:
        if username in watchlist:
            raise HTTPException(409, f"@{username} is already in the watchlist")
        entry = {
            "username": username,
            "mode": req.mode,
            "interval": req.interval,
            "proxy": req.proxy,
            "duration": req.duration,
            "use_cookies": req.use_cookies,
            "added_at": datetime.utcnow().isoformat(),
            "status": "monitoring" if req.mode == "automatic" else "idle",
            "last_seen_live": None,
            "last_error": None,
            "record": req.record,
        }
        watchlist[username] = entry
        _save_watchlist(watchlist)
    if req.mode == "automatic":
        _start_worker(username)
    return entry


@router.delete("/api/users/{username}")
def remove_user(username: str):
    username = username.lstrip("@")
    with _state_lock:
        if username not in watchlist:
            raise HTTPException(404, f"@{username} not found")
    _stop_worker(username)
    with _state_lock:
        watchlist.pop(username, None)
        _save_watchlist(watchlist)
    return {"ok": True, "username": username}


@router.patch("/api/users/{username}")
def update_user(username: str, req: UpdateUserRequest):
    username = username.lstrip("@")
    with _state_lock:
        if username not in watchlist:
            raise HTTPException(404, f"@{username} not found")
        entry = watchlist[username]
        for field in ("mode", "interval", "proxy", "use_cookies", "record"):
            val = getattr(req, field)
            if val is not None:
                entry[field] = val
        _save_watchlist(watchlist)
    # record / use_cookies changes are picked up on the next loop; only
    # restart the worker when it isn't mid-recording
    if entry.get("status") != "recording":
        _stop_worker(username)
        if entry.get("mode") == "automatic":
            _start_worker(username)
    return entry


@router.post("/api/users/{username}/record")
def manual_record(username: str):
    username = username.lstrip("@")
    with _state_lock:
        if username not in watchlist:
            raise HTTPException(404, f"@{username} not found")
        if watchlist[username].get("status") == "recording":
            raise HTTPException(409, f"@{username} is already recording")
        entry = dict(watchlist[username])
        watchlist[username]["status"] = "recording"
        _save_watchlist(watchlist)

    rec_stop = threading.Event()
    _rec_stop_events[username] = rec_stop

    def _do_record():
        code, err = _run_recording(username, entry, [rec_stop])
        if code == 2:
            _set(username, status="idle", last_error="Not live right now")
        else:
            _apply_result(username, code, err, "idle")

    threading.Thread(target=_do_record, daemon=True, name=f"manual-{username}").start()
    return {"ok": True, "message": f"Manual recording triggered for @{username}"}


@router.post("/api/users/{username}/stop")
def stop_recording(username: str):
    username = username.lstrip("@")
    with _state_lock:
        if username not in watchlist:
            raise HTTPException(404, f"@{username} not found")
        status = watchlist[username].get("status")

    if status != "recording":
        raise HTTPException(
            409, f"@{username} is not currently recording (status: {status})"
        )
    if username not in _rec_stop_events:
        raise HTTPException(500, "No active recording event found for this user")

    _rec_stop_events[username].set()
    return {"ok": True, "message": f"Stop signal sent to @{username}'s recording"}


@router.get("/api/users/{username}/status")
def check_live_status(username: str):
    username = username.lstrip("@")
    with _state_lock:
        if username not in watchlist:
            raise HTTPException(404, f"@{username} not found")
        entry = dict(watchlist[username])
    # no need to spawn a check while we're already recording
    if entry.get("status") == "recording":
        return {"username": username, "is_live": True}
    code, err = _check_live(username, entry)
    if code == 0:
        _set(username, last_seen_live=datetime.utcnow().isoformat())
        return {"username": username, "is_live": True}
    if code == 2:
        return {"username": username, "is_live": False}
    return {"username": username, "is_live": False, "error": err}


@router.get("/api/stats")
def get_stats():
    with _state_lock:
        total = len(watchlist)
        recording = sum(1 for e in watchlist.values() if e.get("status") == "recording")
        monitoring = sum(
            1 for e in watchlist.values() if e.get("status") == "monitoring"
        )
    rec_files = list(RECORDINGS_DIR.rglob("*.mp4"))
    disk_mb = sum(f.stat().st_size for f in rec_files) / 1024 / 1024
    return {
        "total_users": total,
        "currently_recording": recording,
        "monitoring": monitoring,
        "total_recordings": len(rec_files),
        "disk_used_mb": round(disk_mb, 1),
    }
