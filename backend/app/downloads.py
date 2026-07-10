import re
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

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


class InvalidDownloadDirError(Exception):
    pass


class DownloadCancelledByUser(Exception):
    pass


# Matches the suffixes yt-dlp appends to its outtmpl-derived filename while a
# download is still in progress:
#   "....mp4.part"            - a single-stream (or already-merged) download in flight
#   "....mp4.part-Frag12"     - one fragment of a DASH/HLS fragmented download
#   "....f137.mp4"            - a finished-but-not-yet-merged video/audio stream
#   "....f137.mp4.part"       - that same stream, still downloading
#   "....f137.mp4.ytdl"       - yt-dlp's fragment-resume metadata sidecar
_INTERMEDIATE_FILE_SUFFIX_RE = re.compile(r"\.part(-Frag\d+)?$|\.f\d+\.[^./\\]+$|\.ytdl$")


def _cleanup_intermediate_files(output_dir: Path, video_id: Optional[str]) -> None:
    """Delete leftover intermediate download files for a single cancelled task.

    Files are matched by the literal "[video_id]" marker that outtmpl embeds in
    every filename it produces, so concurrent downloads of other videos (which
    carry a different ID) are never touched. Only known intermediate-file
    suffixes are removed; a fully merged/completed output file is left alone.
    """
    if not video_id:
        return
    marker = f"[{video_id}]"
    try:
        candidates = list(output_dir.iterdir())
    except OSError:
        return
    for path in candidates:
        name = path.name
        if marker not in name:
            continue
        if not _INTERMEDIATE_FILE_SUFFIX_RE.search(name):
            continue
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass


def _resolve_download_dir(download_dir: Optional[str]) -> Path:
    if not download_dir or not download_dir.strip():
        return DOWNLOAD_DIR

    try:
        target = Path(download_dir.strip()).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InvalidDownloadDirError(f"Invalid download folder: {exc}") from exc

    if not target.is_dir():
        raise InvalidDownloadDirError("Download folder path is not a directory")
    return target


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


def _download_worker(
    task_id: str,
    url: str,
    format_id: Optional[str],
    output_dir: Path,
    merge_output_format: Optional[str],
    subtitle_lang: Optional[str],
    subtitle_auto: bool,
) -> None:
    cancel_event = _cancel_events[task_id]
    # Populated from the progress hook's info_dict as soon as yt-dlp starts
    # downloading, so the cancel handler below knows which files belong to
    # this task (see _cleanup_intermediate_files).
    download_state: Dict[str, Optional[str]] = {"video_id": None}

    def progress_hook(d):
        info_dict = d.get("info_dict") or {}
        video_id = info_dict.get("id")
        if video_id:
            download_state["video_id"] = video_id
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
        # Use the video title (truncated to avoid Windows path-length issues) as the
        # filename, with the video ID appended so intermediate files (.part,
        # .fNNN.*) can be safely identified and cleaned up if the download is
        # canceled, without touching other concurrent tasks' files. Note:
        # re-downloading the same video will overwrite an existing file with
        # the same title+ID.
        "outtmpl": str(output_dir / "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Not using restrictfilenames: it forces ASCII-only names, which would
        # mangle non-Latin (e.g. Japanese) video titles.
        "restrictfilenames": False,
        "progress_hooks": [progress_hook],
        "format": format_id or "bestvideo+bestaudio/best",
        # YouTube now requires solving JS challenges for many videos; without a JS
        # runtime + the official EJS solver script, extraction fails with a
        # spurious "This video is not available" error.
        "js_runtimes": {"deno": {}, "node": {}},
        "remote_components": ["ejs:github"],
        # The default player clients only expose the original audio track; dubbed
        # (multi-language) audio tracks are only listed by the web_embedded client.
        "extractor_args": {"youtube": {"player_client": ["default", "web_embedded"]}},
    }
    if merge_output_format:
        # Forces the merged file into the container the user actually picked
        # (e.g. the format list said "mp4"), instead of yt-dlp's own choice of
        # whatever container best fits the codec combination (e.g. webm/mkv).
        ydl_opts["merge_output_format"] = merge_output_format
    if subtitle_lang:
        ydl_opts["writesubtitles"] = not subtitle_auto
        ydl_opts["writeautomaticsub"] = subtitle_auto
        ydl_opts["subtitleslangs"] = [subtitle_lang]
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegEmbedSubtitle",
            # Delete the standalone subtitle file once it's embedded in the container.
            "already_have_subtitle": False,
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # Determine the actual final output path from yt-dlp itself rather than
            # globbing the directory: a glob can't distinguish this task's output
            # from a leftover file of a prior download of the same video ID (e.g.
            # a different format/container), and could report the wrong file.
            requested = info.get("requested_downloads") or []
            file_path = None
            if requested and requested[-1].get("filepath"):
                file_path = Path(requested[-1]["filepath"])

            with _lock:
                status = DOWNLOADS[task_id]
                status["status"] = "completed"
                status["title"] = info.get("title")
                status["filename"] = file_path.name if file_path else None
                status["file_path"] = str(file_path) if file_path else None
                status["finished_at"] = time.monotonic()
    except DownloadCancelledByUser:
        _cleanup_intermediate_files(output_dir, download_state["video_id"])
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


def start_download(
    url: str,
    format_id: Optional[str],
    download_dir: Optional[str] = None,
    merge_output_format: Optional[str] = None,
    subtitle_lang: Optional[str] = None,
    subtitle_auto: bool = False,
) -> str:
    _cleanup_stale_tasks()
    output_dir = _resolve_download_dir(download_dir)

    task_id = str(uuid.uuid4())
    with _lock:
        active = sum(1 for s in DOWNLOADS.values() if s.get("status") in ACTIVE_STATUSES)
        if active >= MAX_CONCURRENT_DOWNLOADS:
            raise TooManyDownloadsError(
                f"Too many concurrent downloads (max {MAX_CONCURRENT_DOWNLOADS}). Please try again later."
            )
        DOWNLOADS[task_id] = {
            "status": "queued",
            "url": url,
            "download_dir": str(output_dir),
            "created_at": time.monotonic(),
        }
        _cancel_events[task_id] = threading.Event()

    thread = threading.Thread(
        target=_download_worker,
        args=(task_id, url, format_id, output_dir, merge_output_format, subtitle_lang, subtitle_auto),
        daemon=True,
    )
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
        event = _cancel_events.get(task_id)
    if event is None:
        return False
    event.set()
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
