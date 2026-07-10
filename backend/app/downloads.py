import re
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import yt_dlp

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = Path(r"D:\YouTube")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

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
#
# This is now used only to *filter* the small number of paths a task's own
# progress_hook actually observed (see _download_worker), never to scan a
# directory for candidates - so a final, fully-merged output filename (which
# never matches this pattern) is structurally excluded even if a hook fires
# for it, and there's no risk of matching a path that belongs to some other
# task's download.
_INTERMEDIATE_FILE_SUFFIX_RE = re.compile(r"\.part(-Frag\d+)?$|\.f\d+\.[^./\\]+$|\.ytdl$")


def _cleanup_intermediate_files(paths: Iterable[Optional[str]]) -> None:
    """Delete exactly the file paths passed in - no directory scanning.

    Callers are expected to pass only paths that a single task's own
    progress_hook closure actually observed during its own download (see
    _download_worker). Because each task runs its own YoutubeDL instance/
    thread with its own hook closure, those paths are inherently scoped to
    that one task - unlike matching by a "[video_id]" substring, this design
    avoids directory-wide scanning and does not rely on any assumption about
    which characters a site's video IDs may contain.

    Known limitation: if two tasks download the exact same video with the
    exact same format (same outtmpl resolution), they will resolve to an
    identical tmpfilename. In this case (e.g. duplicate concurrent requests,
    or same video+format across multiple browser tabs), canceling one task's
    download would also delete the in-progress file of the other task. The
    app currently has no dedup guard to prevent this (see start_download).
    This is a narrow, accepted limitation similar to the -FragN fragment-file
    limitation noted elsewhere.
    """
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path)
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
    # Every intermediate (not-yet-finished-merging) file path this task's own
    # hook calls have reported, so a cancellation can delete exactly this
    # task's own leftovers (see _cleanup_intermediate_files for the known
    # limitation when another task is downloading the identical video with
    # the identical format).
    observed_files: Set[str] = set()

    def progress_hook(d):
        # Record paths *before* the cancellation check below, so that even the
        # hook call that itself triggers DownloadCancelledByUser has already
        # contributed its paths.
        for key in ("tmpfilename", "filename"):
            candidate = d.get(key)
            if candidate and _INTERMEDIATE_FILE_SUFFIX_RE.search(candidate):
                observed_files.add(candidate)
                # yt-dlp's fragmented-download resume sidecar is named
                # "<final-filename>.ytdl" and lives next to the aggregate
                # ".part" file (see yt_dlp/downloader/fragment.py
                # ytdl_filename()/temp_name()). yt-dlp deletes it itself on a
                # normal finish, but not on cancellation, and it is never
                # reported to hooks directly - so derive it from the matching
                # tmpfilename we did observe rather than scanning the
                # directory for it.
                if key == "tmpfilename" and candidate.endswith(".part"):
                    observed_files.add(candidate[: -len(".part")] + ".ytdl")
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
        # Use the video title (truncated to avoid Windows path-length issues) as
        # the filename, with the video ID appended so two different videos that
        # happen to share a truncated title never overwrite each other's file.
        # Note: this ID suffix is *not* what makes cancellation cleanup safe -
        # that's handled by tracking each task's own observed file paths (see
        # observed_files / _cleanup_intermediate_files below) - so re-downloading
        # the exact same video will still overwrite an existing file with the
        # same title+ID, which is expected.
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
        _cleanup_intermediate_files(observed_files)
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
