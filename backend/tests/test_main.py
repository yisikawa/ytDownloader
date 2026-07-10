import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp
from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app import downloads  # noqa: E402
from app.main import app  # noqa: E402


class MainApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        downloads.DOWNLOADS.clear()
        downloads._cancel_events.clear()

    def test_health_endpoint(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_probe_rejects_non_http_url(self) -> None:
        response = self.client.post("/api/probe", json={"url": "ftp://example.com/video"})
        self.assertEqual(response.status_code, 400)

    def test_download_rejects_non_http_url(self) -> None:
        response = self.client.post("/api/download", json={"url": "ftp://example.com/video"})
        self.assertEqual(response.status_code, 400)

    def test_probe_failure_returns_500(self) -> None:
        with patch("app.routes.yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError("boom")
            response = self.client.post("/api/probe", json={"url": "https://example.com/watch?v=abc"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("boom", response.json()["detail"])

    def test_probe_flags_video_only_formats_and_drops_storyboards(self) -> None:
        fake_info = {
            "id": "abc",
            "title": "Test Video",
            "thumbnail": None,
            "formats": [
                {"format_id": "137", "ext": "mp4", "vcodec": "avc1", "acodec": "none"},
                {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"},
                {"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"},
                {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none"},
            ],
        }
        with patch("app.routes.yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = fake_info
            response = self.client.post("/api/probe", json={"url": "https://example.com/watch?v=abc"})

        self.assertEqual(response.status_code, 200)
        formats = {f["format_id"]: f for f in response.json()["formats"]}
        self.assertNotIn("sb0", formats)
        self.assertEqual(formats["137"], {**formats["137"], "has_video": True, "has_audio": False})
        self.assertEqual(formats["140"], {**formats["140"], "has_video": False, "has_audio": True})
        self.assertEqual(formats["18"], {**formats["18"], "has_video": True, "has_audio": True})

    def test_status_unknown_task_returns_404(self) -> None:
        response = self.client.get("/api/status/does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_cancel_unknown_task_returns_404(self) -> None:
        response = self.client.post("/api/cancel/does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_cancel_already_finished_task_returns_409(self) -> None:
        downloads.DOWNLOADS["t1"] = {"status": "completed", "url": "https://x", "finished_at": time.monotonic()}
        response = self.client.post("/api/cancel/t1")
        self.assertEqual(response.status_code, 409)

    def test_cancel_download_returns_false_when_event_missing(self) -> None:
        # Simulate race condition: task in DOWNLOADS but event was popped from _cancel_events
        # (as would happen if the download finished between cancel_download's status check
        # and its attempt to access the event)
        downloads.DOWNLOADS["t2"] = {"status": "downloading", "url": "https://x"}
        # Intentionally do NOT add an entry to _cancel_events to simulate the race
        result = downloads.cancel_download("t2")
        self.assertFalse(result)

    def test_history_only_includes_terminal_tasks(self) -> None:
        downloads.DOWNLOADS["running"] = {"status": "downloading", "url": "https://a"}
        downloads.DOWNLOADS["done"] = {
            "status": "completed",
            "url": "https://b",
            "title": "video",
            "filename": "b.mp4",
            "finished_at": time.monotonic(),
        }
        response = self.client.get("/api/history")
        self.assertEqual(response.status_code, 200)
        task_ids = [entry["task_id"] for entry in response.json()]
        self.assertIn("done", task_ids)
        self.assertNotIn("running", task_ids)

    def test_start_download_rejects_when_over_concurrency_limit(self) -> None:
        for i in range(downloads.MAX_CONCURRENT_DOWNLOADS):
            downloads.DOWNLOADS[f"active-{i}"] = {"status": "downloading", "url": "https://x"}

        with self.assertRaises(downloads.TooManyDownloadsError):
            downloads.start_download("https://example.com/watch?v=abc", None)

    def test_download_rejects_invalid_download_dir(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            # Point download_dir at a path that already exists as a *file*,
            # so mkdir(parents=True, exist_ok=True) must fail.
            not_a_dir = tmp_file.name

        try:
            response = self.client.post(
                "/api/download",
                json={"url": "https://example.com/watch?v=abc", "download_dir": not_a_dir},
            )
            self.assertEqual(response.status_code, 400)
        finally:
            Path(not_a_dir).unlink(missing_ok=True)

    def test_start_download_records_custom_download_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            custom_dir = str(Path(tmp_dir) / "nested" / "output")
            task_id = downloads.start_download("https://example.com/watch?v=abc", None, custom_dir)
            status = downloads.get_status(task_id)
            self.assertEqual(status["download_dir"], str(Path(custom_dir).resolve()))
            self.assertTrue(Path(custom_dir).is_dir())
            downloads.cancel_download(task_id)

    def test_serve_task_file_unknown_task_returns_404(self) -> None:
        response = self.client.get("/api/files/task/does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_serve_file_rejects_path_traversal(self) -> None:
        # Backslash-escaped traversal reaches our handler and must be rejected explicitly.
        response = self.client.get("/api/files/..%5C..%5Csomefile.txt")
        self.assertEqual(response.status_code, 400)

        # A literal "/" never matches the {filename} route at all, so routing itself
        # denies it (404) before our handler runs — also a safe outcome.
        response = self.client.get("/api/files/../../etc/passwd")
        self.assertEqual(response.status_code, 404)

    def test_cleanup_intermediate_files_removes_only_matching_video_id(self) -> None:
        # Simulates the state of the download folder right after a cancel: this
        # task's own partial/fragment files should be deleted, but a different
        # concurrent task's .part file (different video ID) must survive, and so
        # must an unrelated already-completed file that happens to share this
        # task's ID (e.g. from an earlier, separate successful download).
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)

            this_task_files = [
                "My Video [abc123XYZ_-].f137.mp4.part",
                "My Video [abc123XYZ_-].f251.webm",
                "My Video [abc123XYZ_-].mp4.part-Frag3",
                "My Video [abc123XYZ_-].f137.mp4.ytdl",
            ]
            other_task_file = "Other Video [zzz999AAA00].mp4.part"
            completed_file_same_id = "My Video [abc123XYZ_-].mp4"
            unrelated_file = "notes.txt"

            for name in this_task_files + [other_task_file, completed_file_same_id, unrelated_file]:
                (output_dir / name).write_bytes(b"data")

            downloads._cleanup_intermediate_files(output_dir, "abc123XYZ_-")

            remaining = {p.name for p in output_dir.iterdir()}

            for name in this_task_files:
                self.assertNotIn(name, remaining, f"{name} should have been deleted")
            self.assertIn(other_task_file, remaining)
            self.assertIn(completed_file_same_id, remaining)
            self.assertIn(unrelated_file, remaining)

    def test_cleanup_intermediate_files_noop_without_video_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            (output_dir / "Some Video [abc].mp4.part").write_bytes(b"data")

            downloads._cleanup_intermediate_files(output_dir, None)

            remaining = {p.name for p in output_dir.iterdir()}
            self.assertIn("Some Video [abc].mp4.part", remaining)

    def test_download_worker_cancellation_cleans_up_intermediate_files(self) -> None:
        # End-to-end through _download_worker: a progress_hook call that observes
        # info_dict (yt-dlp always attaches this, see downloader/common.py
        # FileDownloader._hook_progress) captures the video ID, then a cancel
        # request raises DownloadCancelledByUser out of extract_info, and the
        # except-block cleanup should remove this task's partial files while
        # leaving a concurrent task's same-directory .part file untouched.
        task_id = "cancel-cleanup-task"
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            downloads.DOWNLOADS[task_id] = {"status": "downloading", "url": "https://x"}
            downloads._cancel_events[task_id] = threading.Event()

            own_part = output_dir / "My Video [vid12345678].f137.mp4.part"
            own_part.write_bytes(b"partial")
            other_part = output_dir / "Concurrent Video [other999999].f137.mp4.part"
            other_part.write_bytes(b"partial")

            def fake_extract_info(self, url, download=True):
                # Emulate yt-dlp invoking the progress hook mid-download, then
                # the user cancelling before the download completes.
                downloads._cancel_events[task_id].set()
                for hook in self.params["progress_hooks"]:
                    hook({
                        "status": "downloading",
                        "downloaded_bytes": 100,
                        "info_dict": {"id": "vid12345678", "title": "My Video"},
                    })
                raise AssertionError("extract_info should not return normally")

            with patch.object(yt_dlp.YoutubeDL, "extract_info", fake_extract_info):
                downloads._download_worker(task_id, "https://example.com/watch?v=vid12345678", None, output_dir, None, None, False)

            status = downloads.get_status(task_id)
            self.assertEqual(status["status"], "canceled")
            self.assertFalse(own_part.exists(), "this task's partial file should be deleted")
            self.assertTrue(other_part.exists(), "concurrent task's partial file must survive")


if __name__ == "__main__":
    unittest.main()
