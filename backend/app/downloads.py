import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_CONCURRENT_DOWNLOADS = 3
TASK_TTL_SECONDS = 60 * 60  # purge finished tasks after 1 hour
TERMINAL_STATUSES = {"completed", "error", "canceled"}
ACTIVE_STATUSES = {"queued", "downloading"}

# In-memory download tracking, guarded by _lock. For production use a persistent store.
_lock = threading.Lock()
DOWNLOADS: Dict[str, dict] = {}
_cancel_events: Dict[str, threading.Event] = {}


class TooManyDownloadsError(Exception):
    pass


class DownloadCancelledByUser(Exception):
    pass


def _cleanup_stale_tasks() -> None:
    now = time.monotonic()
    with _lock:
        stale = [
            task_id
            for task_id, status in DOWNLOADS.items()
            if status.get("status") in TERMINAL_STATUSES
            and now - status.get("finished_at", now) > TASK_TTL_SECONDS
        ]
        for task_id in stale:
            DOWNLOADS.pop(task_id, None)
            _cancel_events.pop(task_id, None)


def _active_download_count() -> int:
    with _lock:
        return sum(1 for status in DOWNLOADS.values() if status.get("status") in ACTIVE_STATUSES)


def _download_worker(task_id: str, url: str, format_id: Optional[str]) -> None:
    cancel_event = _cancel_events[task_id]

    def progress_hook(d):
        if cancel_event.is_set():
            raise DownloadCancelledByUser("Download canceled by user")
        with _lock:
            status = DOWNLOADS[task_id]
            if d.get("status") == "downloading":
                status["status"] = "downloading"
                status["downloaded_bytes"] = d.get("downloaded_bytes")
                status["total_bytes"] = d.get("total_bytes") or d.get("total_bytes_estimate")
                status["speed"] = d.get("speed")
            elif d.get("status") == "finished":
                status["filename"] = Path(d.get("filename", "")).name

    ydl_opts = {
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "progress_hooks": [progress_hook],
        "format": format_id or "bestvideo+bestaudio/best",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            vid = info.get("id")
            thumb = info.get("thumbnail")
            thumb_name = None
            if thumb:
                try:
                    r = httpx.get(thumb, timeout=10.0)
                    if r.status_code == 200:
                        thumb_path = DOWNLOAD_DIR / f"{vid}.jpg"
                        thumb_path.write_bytes(r.content)
                        thumb_name = thumb_path.name
                except Exception:
                    pass

            filename = None
            for p in DOWNLOAD_DIR.glob(f"{vid}.*"):
                if p.suffix != ".jpg":
                    filename = p.name

            with _lock:
                status = DOWNLOADS[task_id]
                status["status"] = "completed"
                status["title"] = info.get("title")
                status["filename"] = filename
                if thumb_name:
                    status["thumbnail"] = thumb_name
                status["finished_at"] = time.monotonic()
    except DownloadCancelledByUser:
        with _lock:
            status = DOWNLOADS[task_id]
            status["status"] = "canceled"
            status["finished_at"] = time.monotonic()
    except Exception as exc:
        with _lock:
            status = DOWNLOADS[task_id]
            status["status"] = "error"
            status["error"] = str(exc)
            status["finished_at"] = time.monotonic()
    finally:
        _cancel_events.pop(task_id, None)


def start_download(url: str, format_id: Optional[str]) -> str:
    _cleanup_stale_tasks()

    if _active_download_count() >= MAX_CONCURRENT_DOWNLOADS:
        raise TooManyDownloadsError(
            f"Too many concurrent downloads (max {MAX_CONCURRENT_DOWNLOADS}). Please try again later."
        )

    task_id = str(uuid.uuid4())
    with _lock:
        DOWNLOADS[task_id] = {
            "status": "queued",
            "url": url,
            "created_at": time.monotonic(),
        }
        _cancel_events[task_id] = threading.Event()

    thread = threading.Thread(target=_download_worker, args=(task_id, url, format_id), daemon=True)
    thread.start()
    return task_id


def get_status(task_id: str) -> Optional[dict]:
    with _lock:
        status = DOWNLOADS.get(task_id)
        return dict(status) if status is not None else None


def cancel_download(task_id: str) -> bool:
    with _lock:
        status = DOWNLOADS.get(task_id)
        if status is None or status.get("status") in TERMINAL_STATUSES:
            return False
    _cancel_events[task_id].set()
    return True


def list_history(limit: int = 50) -> List[dict]:
    with _lock:
        entries = [
            {"task_id": task_id, **status}
            for task_id, status in DOWNLOADS.items()
            if status.get("status") in TERMINAL_STATUSES
        ]
    entries.sort(key=lambda e: e.get("finished_at", 0), reverse=True)
    return entries[:limit]
