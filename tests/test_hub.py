"""Unit tests for the browser hub (browser/hub.py)."""

import importlib.util
from importlib.machinery import SourceFileLoader
import json
import base64
import socket
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "browser"))


def load(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


hub = load("ai_dev_hub", ROOT / "browser" / "hub.py")


class DesktopStartupTest(unittest.TestCase):
    def test_vnc_and_clipboard_helper_start_on_private_display(self):
        with patch.object(hub, "READY", False), patch.object(hub, "TRANSITION", ""), \
             patch.object(hub, "ensure_dirs"), patch.object(hub.os, "unlink"), \
             patch.object(hub, "spawn") as spawn, patch.object(hub, "wait_port"), \
             patch.object(hub, "dbus_env", return_value={}), \
             patch.object(hub, "copy_fluxbox_config"), \
             patch.object(hub.subprocess, "run", return_value=Mock(returncode=0)):
            hub.start_stack()
            commands = {call.args[0]: call.args[1] for call in spawn.call_args_list}
            self.assertEqual(commands["vnc"][0], "Xtigervnc")
            self.assertIn("-localhost", commands["vnc"])
            self.assertEqual(commands["vncconfig"], ["vncconfig", "-nowin"])
            self.assertTrue(hub.READY)


class RewriteHostTest(unittest.TestCase):
    def test_rewrites_host_header(self):
        raw = (b"GET /json/version HTTP/1.1\r\n"
               b"Host: ai-dev-alice-30-browser:9223\r\n"
               b"Accept: */*\r\n\r\n")
        out = hub.rewrite_host(raw)
        self.assertIn(b"Host: 127.0.0.1:9222", out)
        self.assertNotIn(b"ai-dev-alice-30-browser:9223", out)
        self.assertTrue(out.endswith(b"\r\n\r\n"))

    def test_keeps_body_and_other_headers(self):
        raw = (b"POST /json/new HTTP/1.1\r\nHost: box:9223\r\n"
               b"Content-Length: 5\r\n\r\nhello")
        out = hub.rewrite_host(raw)
        self.assertTrue(out.endswith(b"hello"))
        self.assertIn(b"Content-Length: 5", out)

    def test_incomplete_request_untouched(self):
        raw = b"GET / HTTP/1.1\r\nHost: box"
        self.assertEqual(hub.rewrite_host(raw), raw)


class RequestHeadTest(unittest.TestCase):
    def test_fragmented_head_is_collected_before_rewrite(self):
        raw = b"GET /json/list HTTP/1.1\r\nHost: browser:9223\r\n\r\n"
        reader, writer = socket.socketpair()
        def send():
            with writer:
                for byte in raw:
                    writer.sendall(bytes([byte]))
        thread = threading.Thread(target=send)
        thread.start()
        with reader:
            collected = hub.read_request_head(reader)
        thread.join()
        self.assertEqual(collected, raw)
        self.assertIn(b"Host: 127.0.0.1:9222", hub.rewrite_host(collected))
        self.assertNotIn(b"Host: browser", hub.rewrite_host(collected))

    def test_coalesced_body_is_preserved(self):
        raw = b"POST / HTTP/1.1\r\nHost: box\r\nContent-Length: 4\r\n\r\ndata"
        reader, writer = socket.socketpair()
        with reader, writer:
            writer.sendall(raw)
            self.assertEqual(hub.read_request_head(reader), raw)

    def test_closed_partial_head_is_rejected(self):
        reader, writer = socket.socketpair()
        writer.sendall(b"GET / HTTP/1.1\r\nHost:")
        writer.close()
        with reader, self.assertRaises(ValueError):
            hub.read_request_head(reader)

    def test_header_limit_and_deadline(self):
        reader, writer = socket.socketpair()
        with reader, writer, patch.object(hub, "MAX_HEADER_BYTES", 128):
            writer.sendall(b"GET / HTTP/1.1\r\nX: " + b"a" * 129)
            with self.assertRaises(ValueError):
                hub.read_request_head(reader)
        reader, writer = socket.socketpair()
        with reader, writer, self.assertRaises(TimeoutError):
            hub.read_request_head(reader, timeout=0.01)


class ScreenAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth_file = Path(self.tmp.name) / "screen-auth"
        self.auth_file.write_bytes(b"fixture-user:fixture-password")

    @staticmethod
    def request(value=None):
        header = b"" if value is None else b"Authorization: " + value + b"\r\n"
        return b"GET /vnc.html HTTP/1.1\r\nHost: fixture\r\n" + header + b"\r\n"

    def test_page_and_websocket_use_the_same_credential(self):
        valid = b"Basic " + base64.b64encode(b"fixture-user:fixture-password")
        invalid = b"Basic " + base64.b64encode(b"fixture-user:wrong")
        with patch.object(hub, "AUTH_FILE", str(self.auth_file)):
            self.assertTrue(hub.screen_authorized(self.request(valid)))
            self.assertFalse(hub.screen_authorized(self.request(invalid)))
            self.assertFalse(hub.screen_authorized(self.request()))
            self.assertFalse(hub.screen_authorized(self.request(b"Basic !!!")))
            self.assertFalse(hub.screen_authorized(self.request(valid).replace(
                b"\r\n\r\n", b"\r\nAuthorization: " + valid + b"\r\n\r\n")))

    def test_missing_credential_file_fails_closed(self):
        with patch.object(hub, "AUTH_FILE", str(self.auth_file.parent / "missing")):
            self.assertFalse(hub.screen_authorized(self.request(b"Basic Zml4dHVyZQ==")))

    def test_unauthenticated_page_shows_login_before_waking_desktop(self):
        server, client = socket.socketpair()
        with patch.object(hub, "AUTH_FILE", str(self.auth_file)), \
             patch.object(hub, "ensure_ready") as ensure:
            thread = threading.Thread(target=hub.handle, args=(server, hub.WEBSOCKIFY_PORT, False, False))
            thread.start()
            with client:
                client.settimeout(2)
                client.sendall(self.request())
                response = client.recv(4096)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(response.startswith(b"HTTP/1.1 401"))
            self.assertIn(b'action="/__screen/login"', response)
            self.assertNotIn(b'WWW-Authenticate', response)
            ensure.assert_not_called()

    def test_login_creates_http_only_session_for_page_and_websocket(self):
        server, client = socket.socketpair()
        form = b"username=fixture-user&password=fixture-password"
        request = (b"POST /__screen/login HTTP/1.1\r\nHost: localhost:6080\r\n"
                   b"Origin: http://localhost:6080\r\nContent-Length: " + str(len(form)).encode() + b"\r\n\r\n" + form)
        with server, client, patch.object(hub, "AUTH_FILE", str(self.auth_file)), \
             patch.object(hub, "SCREEN_SESSIONS", {}):
            response = hub.screen_login(server, request)
            self.assertTrue(response.startswith(b"HTTP/1.1 303"))
            cookie = next(line for line in response.split(b"\r\n") if line.startswith(b"Set-Cookie: "))
            self.assertIn(b"HttpOnly", cookie)
            self.assertIn(b"SameSite=Strict", cookie)
            token = cookie.split(b": ", 1)[1].split(b";", 1)[0]
            req = b"GET /websockify HTTP/1.1\r\nHost: localhost:6080\r\nCookie: " + token + b"\r\n\r\n"
            self.assertTrue(hub.screen_session_authorized(req))
            proxied = hub.add_screen_basic_auth(req)
            self.assertTrue(hub.screen_authorized(proxied))
            self.auth_file.write_bytes(b"fixture-user:new-password")
            self.assertFalse(hub.screen_session_authorized(req))

    def test_login_rejects_wrong_password_and_cross_origin(self):
        server, client = socket.socketpair()
        form = b"username=fixture-user&password=wrong"
        with server, client, patch.object(hub, "AUTH_FILE", str(self.auth_file)), \
             patch.object(hub, "SCREEN_SESSIONS", {}):
            for origin, body in ((b"http://localhost:6080", form),
                                 (b"http://attacker.invalid", b"username=fixture-user&password=fixture-password")):
                request = (b"POST /__screen/login HTTP/1.1\r\nHost: localhost:6080\r\nOrigin: " + origin +
                           b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                self.assertTrue(hub.screen_login(server, request).startswith(b"HTTP/1.1 401"))
            self.assertEqual(hub.SCREEN_SESSIONS, {})


class RecoveryTest(unittest.TestCase):
    def test_unhealthy_ready_stack_is_restarted(self):
        with patch.object(hub, "READY", True), patch.object(hub, "NEXT_START", 0), \
             patch.object(hub, "LAST_ERROR", "old"), \
             patch.object(hub, "stack_healthy", return_value=False), \
             patch.object(hub, "stop_stack") as stop, patch.object(hub, "start_stack") as start:
            self.assertTrue(hub.ensure_ready())
            stop.assert_called_once()
            start.assert_called_once()
            self.assertEqual(hub.LAST_ERROR, "")

    def test_start_failure_has_retry_backoff(self):
        with patch.object(hub, "READY", False), patch.object(hub, "PROCS", {}), \
             patch.object(hub, "NEXT_START", 0), patch.object(hub, "LAST_ERROR", ""), \
             patch.object(hub, "stop_stack"), \
             patch.object(hub, "start_stack", side_effect=RuntimeError("fixture failure")) as start:
            self.assertFalse(hub.ensure_ready())
            self.assertFalse(hub.ensure_ready())
            start.assert_called_once()
            self.assertIn("fixture failure", hub.LAST_ERROR)

    def test_dead_component_is_unhealthy(self):
        procs = {name: Mock() for name in hub.REQUIRED_PROCS}
        for proc in procs.values():
            proc.poll.return_value = None
        with patch.object(hub, "PROCS", procs), patch.object(hub, "port_open", return_value=True), \
             patch.object(hub, "vnc_ready", return_value=True):
            self.assertTrue(hub.stack_healthy())
            procs["control"].poll.return_value = 1
            self.assertFalse(hub.stack_healthy())


class HubStatusTest(unittest.TestCase):
    def status_over_socket(self):
        server, client = socket.socketpair()
        thread = threading.Thread(target=hub.handle, args=(server, 7071, False, True))
        thread.start()
        started = time.monotonic()
        with client:
            client.settimeout(1)
            client.sendall(b"GET /__hub/status HTTP/1.1\r\nHost: fixture\r\n\r\n")
            data = b""
            while chunk := client.recv(4096):
                data += chunk
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)
        return json.loads(data.split(b"\r\n\r\n", 1)[1])

    def test_returns_json(self):
        text = hub.hub_status().decode()
        head, _, body = text.partition("\r\n\r\n")
        self.assertIn("200 OK", head)
        payload = json.loads(body)
        self.assertTrue(payload["hub"])
        self.assertIn("stack", payload)
        self.assertIn("idle_timeout", payload)

    def test_degraded_stack_is_not_reported_as_healthy(self):
        with patch.object(hub, "READY", True), patch.object(hub, "stack_healthy", return_value=False):
            head, _, body = hub.hub_status().decode().partition("\r\n\r\n")
        self.assertIn("503", head)
        self.assertEqual(json.loads(body)["state"], "degraded")
        self.assertFalse(json.loads(body)["ok"])

    def test_real_status_request_does_not_refresh_activity(self):
        server, client = socket.socketpair()
        with patch.object(hub, "READY", True), patch.object(hub, "LAST_ERROR", ""), \
             patch.object(hub, "ACTIVE", 0), patch.object(hub, "LAST_ACTIVE", 123.0), \
             patch.object(hub, "stack_healthy", return_value=True), \
             patch.object(hub, "port_open", return_value=True), patch.object(hub, "ensure_ready") as ensure:
            thread = threading.Thread(target=hub.handle, args=(server, 7071, False, True))
            thread.start()
            with client:
                client.settimeout(3)
                client.sendall(b"GET /__hub/status HTTP/1.1\r\nHost: fixture\r\n\r\n")
                data = b""
                while chunk := client.recv(4096):
                    data += chunk
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(hub.LAST_ACTIVE, 123.0)
            self.assertEqual(hub.ACTIVE, 0)
            ensure.assert_not_called()
        payload = json.loads(data.split(b"\r\n\r\n", 1)[1])
        self.assertTrue(payload["chromium"])

    def test_status_responds_during_real_start_lock_contention(self):
        entered, release = threading.Event(), threading.Event()
        def slow_setup():
            entered.set()
            release.wait(3)
            raise RuntimeError("fixture start stopped")
        with patch.object(hub, "READY", False), patch.object(hub, "PROCS", {}), \
             patch.object(hub, "NEXT_START", 0), patch.object(hub, "LAST_ERROR", ""), \
             patch.object(hub, "TRANSITION", ""), patch.object(hub, "ensure_dirs", side_effect=slow_setup):
            worker = threading.Thread(target=hub.ensure_ready)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                status = self.status_over_socket()
                self.assertTrue(status["ok"])
                self.assertEqual(status["state"], "starting")
            finally:
                release.set()
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive())

    def test_status_responds_while_chromium_shutdown_holds_lock(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow_chromium_stop():
            entered.set()
            release.wait(3)
            calls.append("chromium stopped")
        def stop():
            with hub.LOCK:
                hub.stop_stack()
        proc = Mock(pid=12345)
        with patch.object(hub, "READY", True), patch.object(hub, "PROCS", {"control": proc}), \
             patch.object(hub, "TRANSITION", ""), \
             patch.object(hub, "stop_chromium", side_effect=slow_chromium_stop), \
             patch.object(hub.os, "getpgid", return_value=12345), \
             patch.object(hub.os, "killpg", side_effect=lambda *args: calls.append("desktop stopped")), \
             patch.object(hub.os, "unlink"), patch.object(hub, "reap_orphans"), \
             patch.object(hub.subprocess, "run"):
            worker = threading.Thread(target=stop)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                status = self.status_over_socket()
                self.assertTrue(status["ok"])
                self.assertEqual(status["state"], "stopping")
            finally:
                release.set()
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(calls, ["chromium stopped", "desktop stopped"])
            self.assertEqual(hub.TRANSITION, "")
            self.assertFalse(hub.READY)


if __name__ == "__main__":
    unittest.main()
