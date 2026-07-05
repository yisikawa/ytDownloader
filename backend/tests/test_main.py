import sys
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
