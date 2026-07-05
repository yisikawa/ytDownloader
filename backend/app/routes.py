from pathlib import Path

import yt_dlp
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .downloads import (
    DOWNLOAD_DIR,
    InvalidDownloadDirError,
    TooManyDownloadsError,
    cancel_download,
    get_status,
    list_history,
    start_download,
)
from .schemas import DownloadRequest, ProbeRequest

router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/api/probe")
def probe(payload: ProbeRequest):
    if not payload.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Please provide a valid http(s) URL.")

    ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(payload.url, download=False)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Probe failed: {exc}") from exc

    formats = []
    for f in info.get("formats", [])[::-1]:
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        if vcodec == "none" and acodec == "none":
            # storyboard/mhtml entries are not downloadable media, skip them
            continue
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "format_note": f.get("format_note"),
            "resolution": f.get("resolution") or f.get("height"),
            "filesize": f.get("filesize"),
            "tbr": f.get("tbr"),
            "has_video": vcodec != "none",
            "has_audio": acodec != "none",
        })

    return {"id": info.get("id"), "title": info.get("title"), "thumbnail": info.get("thumbnail"), "formats": formats}


@router.post("/api/download")
def download_video(payload: DownloadRequest):
    if not payload.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Please provide a valid http(s) URL.")

    try:
        task_id = start_download(
            payload.url, payload.format_id, payload.download_dir, payload.merge_output_format
        )
    except TooManyDownloadsError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except InvalidDownloadDirError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"task_id": task_id}


@router.post("/api/cancel/{task_id}")
def cancel(task_id: str) -> dict:
    if get_status(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not cancel_download(task_id):
        raise HTTPException(status_code=409, detail="Task already finished")
    return {"canceled": True}


@router.get("/api/status/{task_id}")
def status(task_id: str) -> dict:
    s = get_status(task_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return s


@router.get("/api/history")
def history() -> list:
    return list_history()


@router.get("/api/files/{filename}")
def serve_file(filename: str) -> FileResponse:
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")

    resolved_dir = DOWNLOAD_DIR.resolve()
    file_path = (DOWNLOAD_DIR / filename).resolve()
    if not file_path.is_relative_to(resolved_dir):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(file_path)


@router.get("/api/files/task/{task_id}")
def serve_task_file(task_id: str) -> FileResponse:
    s = get_status(task_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Task not found")

    file_path = s.get("file_path")
    if not file_path or not Path(file_path).is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(file_path)
