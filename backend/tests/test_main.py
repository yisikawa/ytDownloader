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

    def test_probe_download_error_returns_422(self) -> None:
        with patch("app.routes.yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.side_effect = yt_dlp.utils.DownloadError("Video not available")
            response = self.client.post("/api/probe", json={"url": "https://example.com/watch?v=abc"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Video not available", response.json()["detail"])

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

    def test_cleanup_intermediate_files_deletes_only_given_paths(self) -> None:
        # _cleanup_intermediate_files no longer scans a directory or matches by
        # video ID: it deletes exactly the paths it's handed, and nothing else -
        # including files that would have matched the old ID-substring pattern.
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)

            own_part = output_dir / "My Video [abc123XYZ_-].f137.mp4.part"
            own_part.write_bytes(b"data")
            not_passed_but_matching_id = output_dir / "My Video [abc123XYZ_-].f251.webm.part"
            not_passed_but_matching_id.write_bytes(b"data")
            completed_file_same_id = output_dir / "My Video [abc123XYZ_-].mp4"
            completed_file_same_id.write_bytes(b"data")
            missing_path = output_dir / "already-gone.mp4.part"  # never created

            downloads._cleanup_intermediate_files(
                [str(own_part), str(missing_path), None, ""]
            )

            remaining = {p.name for p in output_dir.iterdir()}
            self.assertNotIn(own_part.name, remaining, "the given path should have been deleted")
            self.assertIn(
                not_passed_but_matching_id.name,
                remaining,
                "a file not in the given paths must survive, even if its name matches by ID",
            )
            self.assertIn(completed_file_same_id.name, remaining)

    def test_cleanup_intermediate_files_noop_with_empty_iterable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            survivor = output_dir / "Some Video [abc].mp4.part"
            survivor.write_bytes(b"data")

            downloads._cleanup_intermediate_files([])

            remaining = {p.name for p in output_dir.iterdir()}
            self.assertIn(survivor.name, remaining)

    def test_download_worker_cancellation_cleans_up_intermediate_files(self) -> None:
        # End-to-end through _download_worker: progress_hook calls report this
        # task's own tmpfilename/filename (as yt-dlp's downloader modules do for
        # every downloading/finished hook call - see downloader/http.py and
        # downloader/fragment.py), which get recorded into observed_files. A
        # cancel request then raises DownloadCancelledByUser out of
        # extract_info, and the except-block cleanup removes exactly those
        # observed paths (including the derived .ytdl sidecar) while leaving an
        # unrelated concurrent task's same-directory .part file (a different
        # video, never reported to this task's hook) untouched.
        task_id = "cancel-cleanup-task"
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            downloads.DOWNLOADS[task_id] = {"status": "downloading", "url": "https://x"}
            downloads._cancel_events[task_id] = threading.Event()

            own_tmpfilename = output_dir / "My Video [vid12345678].f137.mp4.part"
            own_tmpfilename.write_bytes(b"partial")
            own_ytdl_sidecar = output_dir / "My Video [vid12345678].f137.mp4.ytdl"
            own_ytdl_sidecar.write_bytes(b"{}")
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
                        "tmpfilename": str(own_tmpfilename),
                        "filename": str(own_tmpfilename)[: -len(".part")],
                        "info_dict": {"id": "vid12345678", "title": "My Video"},
                    })
                raise AssertionError("extract_info should not return normally")

            with patch.object(yt_dlp.YoutubeDL, "extract_info", fake_extract_info):
                downloads._download_worker(task_id, "https://example.com/watch?v=vid12345678", None, output_dir, None, None, False)

            status = downloads.get_status(task_id)
            self.assertEqual(status["status"], "canceled")
            self.assertFalse(own_tmpfilename.exists(), "this task's own tmpfilename should be deleted")
            self.assertFalse(own_ytdl_sidecar.exists(), "the derived .ytdl sidecar should be deleted")
            self.assertTrue(other_part.exists(), "concurrent task's partial file must survive")

    def test_download_worker_cancellation_same_video_id_spares_other_tasks_files(self) -> None:
        # The critical scenario this redesign fixes: two tasks downloading the
        # SAME video concurrently (e.g. a double-click, or two browser tabs)
        # both embed the same "[video_id]" marker in their filenames, since
        # outtmpl includes "%(id)s". Under the old ID-substring-scan design,
        # cancelling task A would have deleted task B's in-progress file too
        # (same ID, same intermediate suffix). With path tracking, task A's
        # cleanup only ever touches paths ITS OWN hook observed, so task B's
        # file survives even though it matches the ID marker.
        task_a = "task-a-same-video"
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            downloads.DOWNLOADS[task_a] = {"status": "downloading", "url": "https://x"}
            downloads._cancel_events[task_a] = threading.Event()

            # Task A is downloading the video-only stream...
            task_a_part = output_dir / "Same Video [dupVID000001].f137.mp4.part"
            task_a_part.write_bytes(b"partial-a")
            # ...while task B (a separate, still-running task/thread, never
            # passed to this call) is downloading the audio-only stream of the
            # very same video ID. This file would match the old marker-based
            # scan too, but task A's hook never saw it.
            task_b_part = output_dir / "Same Video [dupVID000001].f251.webm.part"
            task_b_part.write_bytes(b"partial-b")

            def fake_extract_info(self, url, download=True):
                downloads._cancel_events[task_a].set()
                for hook in self.params["progress_hooks"]:
                    hook({
                        "status": "downloading",
                        "downloaded_bytes": 50,
                        "tmpfilename": str(task_a_part),
                        "filename": str(task_a_part)[: -len(".part")],
                        "info_dict": {"id": "dupVID000001", "title": "Same Video"},
                    })
                raise AssertionError("extract_info should not return normally")

            with patch.object(yt_dlp.YoutubeDL, "extract_info", fake_extract_info):
                downloads._download_worker(
                    task_a, "https://example.com/watch?v=dupVID000001", None, output_dir, None, None, False
                )

            status = downloads.get_status(task_a)
            self.assertEqual(status["status"], "canceled")
            self.assertFalse(task_a_part.exists(), "task A's own observed file should be deleted")
            self.assertTrue(
                task_b_part.exists(),
                "task B's file must survive cancellation of task A, even though it shares the same video ID",
            )


if __name__ == "__main__":
    unittest.main()
