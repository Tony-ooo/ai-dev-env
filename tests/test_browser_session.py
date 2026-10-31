"""Idle expiry, persistent-data boundaries, and fresh-start regressions."""

import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from test_control import control
from test_hub import hub
from test_ai_browser import ai
from test_page_browser import pb
from ai_dev_session import clear_tab_session, expiry_marker, mark_expired


class SessionFilesTest(unittest.TestCase):
    def test_only_restore_records_are_removed_and_cleanup_is_repeatable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            removed = ["Default/Sessions/Session_123", "Default/Sessions/Tabs_456",
                       "Default/Last Session", "Profile 1/Current Tabs"]
            kept = ["Default/Preferences", "Default/Cookies", "Default/Local Storage/data",
                    "Default/Bookmarks", "Default/Sessions/unrelated", "downloads/report.txt"]
            for name in removed + kept:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            mark_expired(str(root / "state"))
            for _ in range(2):
                clear_tab_session(temp)
            self.assertTrue(expiry_marker(str(root / "state")).exists())
            for name in removed:
                self.assertFalse((root / name).exists(), name)
            for name in kept:
                self.assertEqual((root / name).read_text(), "fixture", name)

    @unittest.skipUnless(os.name == "posix", "requires unprivileged symlinks")
    def test_symlinked_session_directory_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "Session_123"
            sentinel.write_text("keep", encoding="utf-8")
            profile = root / "profile" / "Default"
            profile.mkdir(parents=True)
            (profile / "Sessions").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlinked"):
                clear_tab_session(str(root / "profile"))
            self.assertEqual(sentinel.read_text(), "keep")


class IdlePolicyTest(unittest.TestCase):
    def run_reaper(self, *, active=0, elapsed=600, healthy=True):
        with patch.object(hub, "STOPPING", Mock(wait=Mock(side_effect=[False, True]))), \
             patch.object(hub, "READY", True), patch.object(hub, "ACTIVE", active), \
             patch.object(hub, "IDLE_SECONDS", 600), patch.object(hub, "LAST_ACTIVE", 100), \
             patch.object(hub.time, "monotonic", return_value=100 + elapsed), \
             patch.object(hub, "stack_healthy", return_value=healthy), \
             patch.object(hub, "LAST_ERROR", ""), patch.object(hub, "stop_stack") as stop:
            hub.reaper()
            return stop

    def test_threshold_expires_tabs(self):
        self.run_reaper().assert_called_once_with(expire_tabs=True)

    def test_short_gap_keeps_tabs(self):
        self.run_reaper(elapsed=599).assert_not_called()

    def test_connected_viewer_or_inflight_request_prevents_expiry(self):
        self.run_reaper(active=1, elapsed=3600).assert_not_called()

    def test_component_failure_does_not_expire_session(self):
        self.run_reaper(healthy=False).assert_called_once_with()

    def test_failed_shutdown_leaves_expiry_marker_and_does_not_clean_live_profile(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(hub, "STATE_DIR", temp), patch.object(hub, "READY", True), \
             patch.object(hub, "TRANSITION", ""), \
             patch.object(hub, "stop_chromium", side_effect=RuntimeError("still running")), \
             patch.object(hub, "clear_tab_session") as clean:
            with self.assertRaisesRegex(RuntimeError, "still running"):
                hub.stop_stack(expire_tabs=True)
            self.assertTrue(expiry_marker(temp).exists())
            clean.assert_not_called()

    def test_new_request_waits_for_idle_cleanup_before_waking(self):
        stopping, release, woke = threading.Event(), threading.Event(), threading.Event()
        server, client = socket.socketpair()
        def stop(**kwargs):
            self.assertTrue(kwargs["expire_tabs"])
            stopping.set()
            self.assertTrue(release.wait(2))
        def ensure():
            self.assertTrue(release.is_set())
            woke.set()
            return False  # No real desktop is needed to check lifecycle ordering.
        with patch.object(hub, "STOPPING", Mock(wait=Mock(side_effect=[False, True]))), \
             patch.object(hub, "READY", True), patch.object(hub, "ACTIVE", 0), \
             patch.object(hub, "IDLE_SECONDS", 0), patch.object(hub, "LAST_ACTIVE", 0), \
             patch.object(hub, "stack_healthy", return_value=True), \
             patch.object(hub, "stop_stack", side_effect=stop), \
             patch.object(hub, "ensure_ready", side_effect=ensure):
            reaper = threading.Thread(target=hub.reaper)
            request = threading.Thread(target=hub.handle, args=(server, 7071, False, True))
            reaper.start()
            try:
                self.assertTrue(stopping.wait(1))
                request.start()
                client.sendall(b"POST /v1/ensure HTTP/1.1\r\nHost: fixture\r\nContent-Length: 0\r\n\r\n")
                self.assertFalse(woke.wait(0.1))
                release.set()
                client.settimeout(2)
                self.assertIn(b"503", client.recv(4096))
                self.assertTrue(woke.wait(1))
            finally:
                release.set()
                client.close()
                reaper.join(timeout=2)
                request.join(timeout=2)
            self.assertFalse(reaper.is_alive())
            self.assertFalse(request.is_alive())
            self.assertEqual(hub.ACTIVE, 0)


class FreshStartTest(unittest.TestCase):
    def test_expired_start_disables_restore_and_creates_one_blank(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = expiry_marker(temp)
            mark_expired(temp)
            responses = [[], {"id": "blank"}, [{"id": "blank", "type": "page", "url": "about:blank"}]]
            with patch.object(control, "STATE_DIR", temp), \
                 patch.object(control, "cdp_alive", side_effect=[False, True]), \
                 patch.object(control, "chrome_bin", return_value="chromium"), \
                 patch.object(control, "clear_stale_profile_lock", return_value=True), \
                 patch.object(control, "clear_tab_session") as clean, \
                 patch.object(control.os, "makedirs"), patch.object(control, "spawn_logged") as spawn, \
                 patch.object(control.urllib.request, "urlopen",
                              side_effect=[io.StringIO(json.dumps(x)) for x in responses]) as request:
                self.assertTrue(control.ensure_chromium()["started"])
            flags = spawn.call_args.args[0]
            self.assertNotIn("--restore-last-session", flags)
            self.assertIn("--no-startup-window", flags)
            clean.assert_called_once_with(control.PROFILE)
            self.assertEqual(request.call_args_list[1].args[0].method, "PUT")
            self.assertFalse(marker.exists())

    def test_interrupted_initialization_reuses_blank_before_consuming_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            mark_expired(temp)
            page = [{"type": "page", "url": "about:blank"}]
            with patch.object(control, "STATE_DIR", temp), \
                 patch.object(control, "cdp_alive", return_value=True), \
                 patch.object(control.urllib.request, "urlopen",
                              side_effect=[io.StringIO(json.dumps(page)), io.StringIO(json.dumps(page))]) as request:
                self.assertFalse(control.ensure_chromium()["started"])
            self.assertEqual(request.call_count, 2)  # No second PUT/new tab.
            self.assertFalse(expiry_marker(temp).exists())

    def test_failed_validation_keeps_marker_for_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            mark_expired(temp)
            page = [{"type": "page", "url": "https://old.example.test"}]
            with patch.object(control.urllib.request, "urlopen",
                              side_effect=[io.StringIO(json.dumps(page)), io.StringIO(json.dumps(page))]):
                with self.assertRaisesRegex(RuntimeError, "exactly one"):
                    control.finish_fresh_start(expiry_marker(temp))
            self.assertTrue(expiry_marker(temp).exists())

    def test_live_browser_never_has_restore_files_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(control, "STATE_DIR", temp), \
                 patch.object(control, "cdp_alive", return_value=True), \
                 patch.object(control, "clear_tab_session") as clean:
                self.assertFalse(control.ensure_chromium()["started"])
            clean.assert_not_called()


class OpenBlankTest(unittest.TestCase):
    def test_open_reuses_the_only_blank_tab(self):
        for url in ("about:blank", "chrome://newtab/"):
            with self.subTest(url=url), patch.object(ai, "request"), \
                 patch.object(ai, "pages", return_value=[{"id": "blank", "url": url}]), \
                 patch.object(ai, "navigate_tab") as navigate, patch.object(ai, "cdp_json") as cdp, \
                 patch.object(ai, "emit", side_effect=lambda value: value):
                result = ai.cmd_open("https://example.test")
                self.assertTrue(result["reused"])
                navigate.assert_called_once_with("blank", "https://example.test")
                cdp.assert_called_once_with("/json/activate/blank")

    def test_open_preserves_nonblank_tabs(self):
        with patch.object(ai, "request"), \
             patch.object(ai, "pages", return_value=[{"id": "old", "url": "https://old.test"}]), \
             patch.object(ai, "navigate_tab") as navigate, patch.object(ai, "cdp_json", return_value={}) as cdp, \
             patch.object(ai, "emit", side_effect=lambda value: value):
            result = ai.cmd_open("https://new.test")
        self.assertFalse(result["reused"])
        navigate.assert_not_called()
        self.assertEqual(cdp.call_args.args[1], "PUT")


class ShutdownHandshakeTest(unittest.TestCase):
    def test_stalled_handshake_obeys_shutdown_budget_and_can_be_closed(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            finished = threading.Event()
            def accept():
                with server.accept()[0] as conn:
                    conn.recv(4096)
                    finished.wait(1)
            thread = threading.Thread(target=accept)
            thread.start()
            client = pb.CdpClient(f"ws://127.0.0.1:{server.getsockname()[1]}/fixture")
            started = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    client.connect(timeout_s=0.1)
                self.assertLess(time.monotonic() - started, 0.8)
            finally:
                client.close()
                finished.set()
                thread.join(timeout=2)
            self.assertIsNone(client._sock)


if __name__ == "__main__":
    unittest.main()
