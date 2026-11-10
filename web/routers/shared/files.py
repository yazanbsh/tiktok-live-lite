"""
Shared file endpoints — recordings serve/delete, stats, logs.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import subprocess
from .config import (
    RECORDINGS_DIR,
    APP_LOG_FILE,
    DATA_DIR,
    THUMB_RECORDINGS_DIR,
    THUMB_DOWNLOADS_DIR,
    DOWNLOADS_DIR,
)

router = APIRouter()


# ── Recordings ────────────────────────────────────────────────────────────────


@router.get("/api/recordings")
def list_recordings():
    files = []
    for ext in ("*.mp4", "*.flv"):
        for f in RECORDINGS_DIR.rglob(ext):
            stat = f.stat()
            username = f.parent.name if f.parent != RECORDINGS_DIR else "unknown"
            files.append(
                {
                    "filename": f.name,
                    "username": username,
                    "size_mb": round(stat.st_size / 1024 / 1024, 2),
                    "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "path": str(f),
                }
            )
    files.sort(key=lambda x: x["created_at"], reverse=True)
    return files


@router.get("/api/recordings/{username}/{filename}")
def serve_recording(username: str, filename: str, inline: bool = False):
    file_path = RECORDINGS_DIR / username / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "File not found")
    try:
        file_path.relative_to(RECORDINGS_DIR)
    except ValueError:
        raise HTTPException(403, "Access denied")
    disposition = (
        f'inline; filename="{filename}"'
        if inline
        else f'attachment; filename="{filename}"'
    )
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="video/mp4",
        headers={"Content-Disposition": disposition},
    )


class BatchDeleteRecordingsRequest(BaseModel):
    files: list[str]


@router.delete("/api/recordings")
def batch_delete_recordings(req: BatchDeleteRecordingsRequest):
    deleted, failed = [], []
    for entry in req.files:
        try:
            parts = entry.split("/", 1)
            if len(parts) != 2:
                failed.append({"file": entry, "error": "Invalid path format"})
                continue
            username, filename = parts
            file_path = RECORDINGS_DIR / username / filename
            file_path.relative_to(RECORDINGS_DIR)
            if not file_path.exists():
                failed.append({"file": entry, "error": "File not found"})
                continue
            file_path.unlink()
            deleted.append(entry)
        except ValueError:
            failed.append({"file": entry, "error": "Access denied"})
        except Exception as e:
            failed.append({"file": entry, "error": str(e)})
    return {"deleted": deleted, "failed": failed}


# ── Thumbnails ───────────────────────────────────────────────────────────────


def _generate_thumbnail(video_path: Path, thumb_path: Path) -> bool:
    """Generate a thumbnail using ffmpeg. Tries at 5s, 1s, then first frame."""
    try:
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        for seek in ("00:00:05", "00:00:01", "00:00:00"):
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss", seek,
                    "-i", str(video_path),
                    "-vframes", "1",
                    "-vf", "scale=320:-1",
                    str(thumb_path),
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0 and thumb_path.exists():
                return True
        return False
    except Exception:
        return False


def _thumb_endpoint(video_dir: Path, thumb_dir: Path, username: str, filename: str):
    """Shared thumbnail serve/generate logic."""
    # for downloads: check root dir first, then pics/ subdir
    video_path = video_dir / username / filename
    if not video_path.exists() and video_dir == DOWNLOADS_DIR:
        video_path = video_dir / username / "pics" / filename

    if not video_path.exists():
        raise HTTPException(404, "Video not found")
    try:
        video_path.relative_to(video_dir)
    except ValueError:
        raise HTTPException(403, "Access denied")

    # images (jpg/png) are their own thumbnail — just serve them directly
    if video_path.suffix.lower() in (".jpg", ".jpeg", ".png"):
        return FileResponse(path=str(video_path), media_type="image/jpeg")

    thumb_name = Path(filename).stem + ".jpg"
    thumb_path = thumb_dir / username / thumb_name

    if not thumb_path.exists():
        if not _generate_thumbnail(video_path, thumb_path):
            raise HTTPException(500, "Failed to generate thumbnail")

    return FileResponse(path=str(thumb_path), media_type="image/jpeg")


@router.get("/api/recordings/{username}/{filename}/thumbnail")
def recording_thumbnail(username: str, filename: str):
    return _thumb_endpoint(RECORDINGS_DIR, THUMB_RECORDINGS_DIR, username, filename)


@router.get("/api/tiktok/downloads/{username}/{filename}/thumbnail")
def download_thumbnail(username: str, filename: str):
    return _thumb_endpoint(DOWNLOADS_DIR, THUMB_DOWNLOADS_DIR, username, filename)


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.get("/api/logs")
def get_logs(lines: int = 200):
    """Serve app.log (Python logging)."""
    if not APP_LOG_FILE.exists():
        return {"lines": []}
    try:
        all_lines = APP_LOG_FILE.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        return {"lines": all_lines[-lines:]}
    except Exception as e:
        return {"lines": [], "error": str(e)}
