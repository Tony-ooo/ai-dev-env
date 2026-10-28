"""Isolated runtime checks: real sockets, subprocesses and temporary files only."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from test_control import control
from test_hub import hub

import ai_dev_logs

ROOT = Path(__file__).resolve().parents[1]


class RuntimeLogTest(unittest.TestCase):
    def test_rotation_is_bounded_during_continuous_output(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "component.log"
            path.write_bytes(b"old" * 300)
            (Path(temp) / "component.log.1").write_bytes(b"older" * 200)
            ai_dev_logs.capture_log(io.BytesIO(b"a" * 501 + b"END"), str(path), limit=100)
            self.assertLessEqual(path.stat().st_size, 100)
            self.assertLessEqual(Path(str(path) + ".1").stat().st_size, 100)
            self.assertTrue(path.read_bytes().endswith(b"END"))

    def test_real_child_keeps_running_with_bounded_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "child.log"
            child = ai_dev_logs.spawn_logged(
                [sys.executable, "-c", "import sys,time; sys.stdout.buffer.write(b'x' * 6000000 + b'END'); sys.stdout.flush(); time.sleep(0.5)"],
                str(path),
            )
            self.assertEqual(child.wait(timeout=5), 0)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if path.exists() and path.read_bytes().endswith(b"END"):
                    break
                time.sleep(0.01)
            self.assertTrue(path.read_bytes().endswith(b"END"))
            self.assertLessEqual(path.stat().st_size, ai_dev_logs.LOG_LIMIT)
            self.assertLessEqual(Path(str(path) + ".1").stat().st_size, ai_dev_logs.LOG_LIMIT)

    def test_log_failure_drains_child_output_and_reports_once(self):
        with tempfile.TemporaryDirectory() as temp:
            invalid_dir = Path(temp) / "file"
            invalid_dir.write_text("not a directory", encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                child = ai_dev_logs.spawn_logged(
                    [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 6000000)"],
                    str(invalid_dir / "child.log"),
                )
                self.assertEqual(child.wait(timeout=5), 0)
            self.assertEqual(stderr.getvalue().count("discarding output"), 1)


@unittest.skipUnless(sys.platform == "linux" and shutil.which("pgrep"), "requires procps on Linux")
class RealProcessLookupTest(unittest.TestCase):
    def test_exited_unreaped_chromium_is_not_a_live_shutdown_target(self):
        # Give the disposable child Chromium's comm name, then intentionally
        # leave it unreaped to reproduce the old pgrep-only shutdown delay.
        child = subprocess.Popen([
            sys.executable, "-c",
            "import ctypes;ctypes.CDLL(None).prctl(15,b'chromium-audit',0,0,0)",
        ])
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                stat = Path(f"/proc/{child.pid}/stat").read_text()
                if stat.split(")", 1)[1].split()[0] == "Z":
                    break
                time.sleep(0.01)
            else:
                self.fail("Fixture process did not become a zombie")
            old = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", "chromium"], capture_output=True, text=True)
            self.assertIn(str(child.pid), old.stdout.splitlines())
            self.assertFalse(hub.chromium_alive())
        finally:
            child.wait(timeout=3)

    def test_live_process_pattern_and_stale_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = str(Path(temp) / "profile.with.regex[chars]")
            Path(profile).mkdir()
            lock = Path(profile) / "SingletonLock"
            lock.write_text("fixture", encoding="utf-8")
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)",
                                      "--user-data-dir=" + profile])
            try:
                with patch.object(control, "PROFILE", profile):
                    self.assertTrue(control.chromium_running())
                    self.assertFalse(control.clear_stale_profile_lock())
                    self.assertTrue(lock.exists())
                    child.terminate()
                    child.wait(timeout=3)
                    self.assertFalse(control.chromium_running())
                    self.assertTrue(control.clear_stale_profile_lock())
                    self.assertFalse(lock.exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()


class ControlHttpTest(unittest.TestCase):
    def test_http_failure_preserves_partial_progress(self):
        server = control.ThreadingHTTPServer(("127.0.0.1", 0), control.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(control, "TOKEN", "fixture-token"), \
                 patch.object(control, "xdotool", side_effect=[None, RuntimeError("fixture failure")]):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/actions",
                    data=json.dumps({"actions": [{"type": "key", "key": "Return"},
                                                 {"type": "key", "key": "Tab"}]}).encode(),
                    headers={"Authorization": "Bearer fixture-token", "Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=3)
                with caught.exception as response:
                    result = json.loads(response.read())
                self.assertEqual(caught.exception.code, 500)
                self.assertEqual(result["completed"], 1)
                self.assertTrue(result["uncertain"])
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


@unittest.skipUnless(os.name == "posix", "requires isolated Linux filesystem")
class BrowserStartMigrationTest(unittest.TestCase):
    def run_start(self, fail_copy=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name) / "home"
        (home / "Downloads").mkdir(parents=True)
        (home / "Downloads/file.txt").write_text("original", encoding="utf-8")
        bin_dir = Path(temp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bin_dir / "python3").chmod(0o755)
        if fail_copy:
            (bin_dir / "cp").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (bin_dir / "cp").chmod(0o755)
        env = {"HOME": str(home), "PATH": str(bin_dir) + ":/usr/bin:/bin", "BROWSER_CONTROL_TOKEN": "fixture-token"}
        result = subprocess.run(["/bin/bash", str(ROOT / "browser/start.sh")], env=env,
                                capture_output=True, text=True, timeout=5)
        return result, home

    def test_failed_copy_keeps_original_downloads(self):
        result, home = self.run_start(fail_copy=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((home / "Downloads").is_symlink())
        self.assertEqual((home / "Downloads/file.txt").read_text(), "original")

    def test_success_moves_downloads_to_shared_directory(self):
        result, home = self.run_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((home / "Downloads").is_symlink())
        self.assertEqual((home / ".ai-data/downloads/file.txt").read_text(), "original")


if __name__ == "__main__":
    unittest.main()
