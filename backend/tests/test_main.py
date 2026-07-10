import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
