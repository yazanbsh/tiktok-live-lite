"""
TikTok on-demand video downloader — queue, SSE progress, tikwm integration.
"""

import asyncio
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..shared.config import DOWNLOADS_DIR, QUEUE_FILE, TIKWM_SUBMIT, TIKWM_RESULT

TIKWM_DEFAULT_API = "https://www.tikwm.com/api/"

router = APIRouter()

# ── Queue persistence ─────────────────────────────────────────────────────────

_queue_lock = threading.Lock()


def _load_queue() -> list:
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text())
        except Exception:
            return []
    return []


def _save_queue(queue: list):
    QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def _queue_update_item(item_id: str, **kwargs):
    with _queue_lock:
        for item in _dl_queue:
            if item["id"] == item_id:
                item.update(kwargs)
                break
        _save_queue(_dl_queue)


# Load on startup — reset processing → interrupted
_dl_queue: list = _load_queue()
with _queue_lock:
    for _item in _dl_queue:
        if _item.get("status") == "processing":
            _item["status"] = "interrupted"
    _save_queue(_dl_queue)

# ── SSE subscribers ───────────────────────────────────────────────────────────

_sse_subscribers: set = set()


def _broadcast(event: dict):
    for q in list(_sse_subscribers):
        try:
            q.put_nowait(event)
        except Exception:
            pass


# ── Download log helpers ──────────────────────────────────────────────────────


def _dl_log_path(username: str) -> Path:
    return DOWNLOADS_DIR / username / f"{username}_log.json"


def _dl_error_log_path(username: str) -> Path:
    return DOWNLOADS_DIR / username / f"{username}_errors.log"


def _load_dl_log(username: str) -> dict:
    p = _dl_log_path(username)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_dl_log(username: str, log: dict):
    p = _dl_log_path(username)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(log, indent=2))


def _append_dl_error(username: str, message: str):
    p = _dl_error_log_path(username)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(f"[{datetime.utcnow().isoformat()}] {message}\n")


# ── URL helpers ───────────────────────────────────────────────────────────────


async def _resolve_url(url: str, client: httpx.AsyncClient) -> str:
    try:
        resp = await client.get(url, follow_redirects=True, timeout=15)
        return str(resp.url)
    except Exception:
        return url


def _extract_video_id(url: str) -> Optional[str]:
    for pattern in [r"/video/(\d+)", r"/photo/(\d+)"]:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def _extract_username(url: str) -> Optional[str]:
    m = re.search(r"/@([\w.]+)/", url)
    if m:
        return m.group(1)
    return None


async def _get_video_id_and_username(url: str, client: httpx.AsyncClient):
    resolved = url
    if "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        resolved = await _resolve_url(url, client)
    return _extract_video_id(resolved), _extract_username(resolved), resolved


# ── tikwm helpers ─────────────────────────────────────────────────────────────


async def _submit_tikwm_task(url: str, client: httpx.AsyncClient) -> Optional[str]:
    try:
        resp = await client.post(TIKWM_SUBMIT, data={"url": url}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0 and data.get("data", {}).get("task_id"):
            return data["data"]["task_id"]
        return None
    except Exception as e:
        raise RuntimeError(f"tikwm submit failed: {e}")


async def _poll_tikwm_result(
    task_id: str, client: httpx.AsyncClient, max_attempts: int = 15, delay: float = 0.5
) -> Optional[dict]:
    for _ in range(max_attempts):
        try:
            resp = await client.get(
                TIKWM_RESULT, params={"task_id": task_id}, timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0 and data.get("data"):
                task_data = data["data"]
                detail = task_data.get("detail", {})
                if task_data.get("status") == 2 and detail.get("play_url"):
                    return detail
        except Exception:
            pass
        await asyncio.sleep(delay)
    return None


async def _download_file(url: str, dest: Path, client: httpx.AsyncClient):
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with client.stream("GET", url, timeout=120, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in resp.aiter_bytes(8192):
                f.write(chunk)


# ── Photo post downloader ────────────────────────────────────────────────────


async def _process_photo_post(
    item_id: str,
    url: str,
    video_id: str,
    username: str,
    client: httpx.AsyncClient,
    update_fn,
) -> bool:
    """
    Download all images from a TikTok photo post via tikwm default API.
    Returns True on success.
    """
    import logging

    log = logging.getLogger(__name__)

    try:
        resp = await client.get(
            TIKWM_DEFAULT_API,
            params={"url": video_id, "hd": "1"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        update_fn("error", f"tikwm photo API failed: {e}")
        return False

    if data.get("code") != 0:
        update_fn("error", f"tikwm photo API error: {data.get('msg', 'unknown')}")
        return False

    images = data.get("data", {}).get("images") or []
    if not images:
        update_fn("error", "No images found in tikwm response")
        return False

    # use username from API response if available
    api_username = data.get("data", {}).get("author", {}).get("unique_id") or username
    # save pics to username/pics/ subdir
    pics_dir = DOWNLOADS_DIR / api_username / "pics"
    pics_dir.mkdir(parents=True, exist_ok=True)

    total = len(images)
    filenames = []
    downloaded_at = datetime.utcnow().isoformat()

    for idx, img_url in enumerate(images, start=1):
        update_fn("processing", f"downloading {idx}/{total} images")
        filename = f"{api_username}_{video_id}_{idx}.jpg"
        dest = pics_dir / filename
        try:
            await _download_file(img_url, dest, client)
            filenames.append(filename)
            log.info(f"[{video_id}] downloaded image {idx}/{total}")
        except Exception as e:
            update_fn("error", f"Image {idx}/{total} failed: {e}")
            _append_dl_error(api_username, f"[{video_id}] image {idx} error: {e}")
            # clean up downloaded so far
            for f in filenames:
                p = pics_dir / f
                if p.exists():
                    p.unlink()
            return False

    # log each pic individually as POSTID_1, POSTID_2, etc.
    dl_log = _load_dl_log(api_username)
    for idx, filename in enumerate(filenames, start=1):
        pic_key = f"{video_id}_{idx}"
        dl_log[pic_key] = {
            "video_id": pic_key,
            "original_url": url,
            "filename": filename,
            "type": "photo",
            "downloaded_at": downloaded_at,
        }
    _save_dl_log(api_username, dl_log)

    _queue_update_item(
        item_id,
        status="downloaded",
        reason=f"{total} images",
        video_id=video_id,
        username=api_username,
        filename=filenames[0] if filenames else "",
    )
    _broadcast(
        {
            "id": item_id,
            "status": "downloaded",
            "reason": f"{total} images",
            "video_id": video_id,
            "username": api_username,
            "filename": filenames[0] if filenames else "",
        }
    )
    return True


# ── Core processor ────────────────────────────────────────────────────────────


async def _process_queue_item(item: dict):
    item_id = item["id"]
    url = item["url"]

    def update(status, reason="", **extra):
        _queue_update_item(item_id, status=status, reason=reason, **extra)
        _broadcast({"id": item_id, "status": status, "reason": reason, **extra})

    update("processing")

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True
        ) as client:
            video_id, username, resolved_url = await _get_video_id_and_username(
                url, client
            )

            if not video_id:
                update("error", "Could not extract video ID from URL")
                return
            if not username:
                update("error", "Could not extract username from URL")
                return

            log = _load_dl_log(username)
            if video_id in log:
                update(
                    "skipped", f"Already downloaded on {log[video_id]['downloaded_at']}"
                )
                return

            with _queue_lock:
                already = any(
                    i["id"] != item_id
                    and i.get("video_id") == video_id
                    and i.get("status") == "downloaded"
                    for i in _dl_queue
                )
            if already:
                update("skipped", "Duplicate — already downloaded in this session")
                return

            # ── route: photo post vs video post ───────────────────────────
            is_photo = "/photo/" in resolved_url

            if is_photo:
                await _process_photo_post(
                    item_id, url, video_id, username, client, update
                )
                return

            # ── video post: tikwm task flow ───────────────────────────────
            try:
                task_id = await _submit_tikwm_task(resolved_url, client)
            except RuntimeError as e:
                update("error", str(e))
                _append_dl_error(username, f"[{video_id}] {e}")
                return

            if not task_id:
                update(
                    "error",
                    "tikwm did not return a task_id (video may not exist or is private)",
                )
                _append_dl_error(username, f"[{video_id}] no task_id")
                return

            detail = await _poll_tikwm_result(task_id, client)
            if not detail:
                update("error", "tikwm task did not complete after 15 attempts")
                _append_dl_error(username, f"[{video_id}] task {task_id} timed out")
                return

            play_url = detail.get("play_url")
            api_username = detail.get("author", {}).get("unique_id") or username
            filename = f"{api_username}_{video_id}.mp4"
            dest = DOWNLOADS_DIR / api_username / filename

            try:
                await _download_file(play_url, dest, client)
            except Exception as e:
                update("error", f"Download failed: {e}")
                _append_dl_error(api_username, f"[{video_id}] {e}")
                if dest.exists():
                    dest.unlink()
                return

            log = _load_dl_log(api_username)
            log[video_id] = {
                "video_id": video_id,
                "original_url": url,
                "filename": filename,
                "downloaded_at": datetime.utcnow().isoformat(),
            }
            _save_dl_log(api_username, log)
            _queue_update_item(
                item_id,
                status="downloaded",
                reason="OK",
                video_id=video_id,
                username=api_username,
                filename=filename,
            )
            _broadcast(
                {
                    "id": item_id,
                    "status": "downloaded",
                    "reason": "OK",
                    "video_id": video_id,
                    "username": api_username,
                    "filename": filename,
                }
            )

    except Exception as e:
        update("error", f"Unexpected error: {e}")


# ── Background worker ─────────────────────────────────────────────────────────

_worker_thread: Optional[threading.Thread] = None
_worker_event = threading.Event()
_worker_started = False


def _queue_worker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        item = None
        with _queue_lock:
            for i in _dl_queue:
                if i.get("status") == "waiting":
                    item = i
                    break
        if item:
            loop.run_until_complete(_process_queue_item(item))
        else:
            _worker_event.wait(timeout=2)
            _worker_event.clear()


def _ensure_worker():
    global _worker_thread
    if not _worker_started:
        return
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_queue_worker, daemon=True)
        _worker_thread.start()


def _start_dl_worker():
    global _worker_started
    _worker_started = True
    _ensure_worker()
    _worker_event.set()


# ── Routes ────────────────────────────────────────────────────────────────────


class DownloadRequest(BaseModel):
    urls: list[str]


@router.post("/api/tiktok/downloads")
def submit_downloads(req: DownloadRequest):
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "No URLs provided")

    added = []
    seen_in_req: set = set()
    with _queue_lock:
        queued_urls = {i["url"] for i in _dl_queue}
        for url in urls:
            if url in seen_in_req or url in queued_urls:
                continue
            seen_in_req.add(url)
            item = {
                "id": str(uuid.uuid4()),
                "url": url,
                "status": "waiting",
                "reason": "",
                "video_id": None,
                "username": None,
                "filename": None,
                "added_at": datetime.utcnow().isoformat(),
            }
            _dl_queue.append(item)
            added.append(item)
        _save_queue(_dl_queue)

    for item in added:
        _broadcast(
            {
                "id": item["id"],
                "status": "waiting",
                "url": item["url"],
                "added_at": item["added_at"],
            }
        )

    _start_dl_worker()
    return {"queued": len(added), "items": added}


@router.get("/api/tiktok/downloads/stream")
async def downloads_stream():
    q: asyncio.Queue = asyncio.Queue()
    _sse_subscribers.add(q)

    async def event_gen():
        try:
            with _queue_lock:
                snapshot = list(_dl_queue)
            yield f"data: {json.dumps({'type': 'snapshot', 'items': snapshot})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps({'type': 'update', **event})}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        except Exception:
            pass
        finally:
            _sse_subscribers.discard(q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.post("/api/tiktok/downloads/resume-queue")
def resume_full_queue():
    with _queue_lock:
        count = 0
        for item in _dl_queue:
            if item["status"] == "interrupted":
                item["status"] = "waiting"
                item["reason"] = ""
                count += 1
                _broadcast({"id": item["id"], "status": "waiting", "reason": ""})
        waiting = sum(1 for i in _dl_queue if i["status"] == "waiting")
        if waiting == 0:
            raise HTTPException(400, "No waiting or interrupted items in queue")
        _save_queue(_dl_queue)
    _start_dl_worker()
    return {"ok": True, "waiting": waiting, "resumed_interrupted": count}


class ClearQueueRequest(BaseModel):
    statuses: list[str]


@router.delete("/api/tiktok/downloads/queue")
def clear_queue_by_status(req: ClearQueueRequest):
    with _queue_lock:
        removed_ids = [i["id"] for i in _dl_queue if i["status"] in req.statuses]
        _dl_queue[:] = [i for i in _dl_queue if i["status"] not in req.statuses]
        _save_queue(_dl_queue)
    for item_id in removed_ids:
        _broadcast({"id": item_id, "status": "removed"})
    return {"removed": len(removed_ids)}


@router.delete("/api/tiktok/downloads/queue/{item_id}")
def remove_queue_item(item_id: str):
    with _queue_lock:
        item = next((i for i in _dl_queue if i["id"] == item_id), None)
        if not item:
            raise HTTPException(404, "Queue item not found")
        if item["status"] != "waiting":
            raise HTTPException(
                409, f"Cannot remove item with status '{item['status']}'"
            )
        _dl_queue.remove(item)
        _save_queue(_dl_queue)
    _broadcast({"id": item_id, "status": "removed"})
    return {"ok": True}


@router.post("/api/tiktok/downloads/queue/{item_id}/resume")
def resume_queue_item(item_id: str):
    with _queue_lock:
        item = next((i for i in _dl_queue if i["id"] == item_id), None)
        if not item:
            raise HTTPException(404, "Queue item not found")
        if item["status"] not in ("interrupted", "error"):
            raise HTTPException(
                409, f"Cannot resume item with status '{item['status']}'"
            )
        item["status"] = "waiting"
        item["reason"] = ""
        _save_queue(_dl_queue)
    _broadcast({"id": item_id, "status": "waiting", "reason": ""})
    _start_dl_worker()
    return {"ok": True}


# ── Import endpoints ─────────────────────────────────────────────────────────


class ImportReviewRequest(BaseModel):
    username: str
    filenames: list[str]  # original filenames from client


@router.post("/api/tiktok/imports/review")
def review_import(req: ImportReviewRequest):
    """
    Check which files already exist in the user's log.
    Returns list of {filename, video_id, new_filename, status}
    """
    username = req.username.strip().lstrip("@")
    if not username:
        raise HTTPException(400, "Username required")

    log = _load_dl_log(username)
    results = []

    for original in req.filenames:
        # extract video ID: "7330367233635421445_HD.mp4" → "7330367233635421445"
        stem = Path(original).stem  # e.g. "7330367233635421445_HD"
        video_id = stem.replace("_HD", "").replace("_hd", "").strip("_")
        # fallback: if no underscore pattern just use stem
        if not video_id.isdigit():
            # try splitting on underscore and taking first numeric part
            parts = stem.split("_")
            video_id = next((p for p in parts if p.isdigit()), stem)

        new_filename = f"{username}_{video_id}.mp4"
        already_in_log = video_id in log
        dest = DOWNLOADS_DIR / username / new_filename
        already_on_disk = dest.exists()

        results.append(
            {
                "original_filename": original,
                "video_id": video_id,
                "new_filename": new_filename,
                "status": "exists" if (already_in_log or already_on_disk) else "ready",
            }
        )

    return {"username": username, "files": results}


from fastapi import UploadFile, File, Form


@router.post("/api/tiktok/imports/file")
async def import_single_file(
    username: str = Form(...),
    video_id: str = Form(...),
    new_filename: str = Form(...),
    original_filename: str = Form(...),
    force: bool = Form(False),
    file: UploadFile = File(...),
):
    """
    Import a single file. Called once per file from the frontend loop.
    Returns status: imported | skipped | error
    """
    username = username.strip().lstrip("@")
    user_dir = DOWNLOADS_DIR / username
    user_dir.mkdir(parents=True, exist_ok=True)

    dest = user_dir / new_filename
    log = _load_dl_log(username)

    already_in_log = video_id in log
    already_on_disk = dest.exists()

    if (already_in_log or already_on_disk) and not force:
        return {
            "status": "skipped",
            "filename": new_filename,
            "reason": "Already exists",
        }

    try:
        contents = await file.read()
        dest.write_bytes(contents)
        log[video_id] = {
            "video_id": video_id,
            "original_url": f"imported:{original_filename}",
            "filename": new_filename,
            "downloaded_at": datetime.utcnow().isoformat(),
        }
        _save_dl_log(username, log)
        return {"status": "imported", "filename": new_filename}
    except Exception as e:
        if dest.exists():
            dest.unlink()
        return {"status": "error", "filename": new_filename, "reason": str(e)}


@router.get("/api/tiktok/downloads")
def list_downloads():
    files = []
    for pattern in ("*.mp4", "*.jpg", "*.jpeg", "*.png"):
        for f in DOWNLOADS_DIR.rglob(pattern):
            if f.name.endswith("_log.json") or f.name.endswith("_errors.log"):
                continue
            stat = f.stat()
            # username is always the first-level subdir under DOWNLOADS_DIR
            try:
                rel = f.relative_to(DOWNLOADS_DIR)
                username = rel.parts[0]
            except Exception:
                username = f.parent.name
            files.append(
                {
                    "filename": f.name,
                    "username": username,
                    "size_mb": round(stat.st_size / 1024 / 1024, 2),
                    "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "subdir": f.parent.name if f.parent.name != username else None,
                }
            )
    files.sort(key=lambda x: x["created_at"], reverse=True)
    return files


@router.get("/api/tiktok/downloads/{username}/{filename}")
def serve_download(username: str, filename: str, inline: bool = False):
    # check root dir first, then pics/ subdir
    file_path = DOWNLOADS_DIR / username / filename
    if not file_path.exists():
        file_path = DOWNLOADS_DIR / username / "pics" / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "File not found")
    try:
        file_path.relative_to(DOWNLOADS_DIR)
    except ValueError:
        raise HTTPException(403, "Access denied")
    ext = file_path.suffix.lower()
    media_type = {
        ".mp4": "video/mp4",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(ext, "application/octet-stream")
    disposition = (
        f'inline; filename="{filename}"'
        if inline
        else f'attachment; filename="{filename}"'
    )
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


class BatchDeleteDownloadsRequest(BaseModel):
    files: list[str]


@router.delete("/api/tiktok/downloads")
def batch_delete_downloads(req: BatchDeleteDownloadsRequest):
    deleted, failed = [], []
    for entry in req.files:
        try:
            parts = entry.split("/", 1)
            if len(parts) != 2:
                failed.append({"file": entry, "error": "Invalid path"})
                continue
            username, filename = parts
            # check root dir first, then pics/ subdir (same as serve_download)
            file_path = DOWNLOADS_DIR / username / filename
            if not file_path.exists():
                file_path = DOWNLOADS_DIR / username / "pics" / filename
            file_path.relative_to(DOWNLOADS_DIR)
            if not file_path.exists():
                failed.append({"file": entry, "error": "File not found"})
                continue
            log = _load_dl_log(username)
            # match by filename — works for both videos and individual pics
            vid = next(
                (v for v, m in log.items() if m.get("filename") == filename), None
            )
            if vid:
                del log[vid]
                _save_dl_log(username, log)
            file_path.unlink()
            deleted.append(entry)
        except ValueError:
            failed.append({"file": entry, "error": "Access denied"})
        except Exception as e:
            failed.append({"file": entry, "error": str(e)})
    return {"deleted": deleted, "failed": failed}
