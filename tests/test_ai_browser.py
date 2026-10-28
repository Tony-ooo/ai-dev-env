"""Unit tests for browser/ai-browser (env file + port resolution)."""

import importlib.util
from importlib.machinery import SourceFileLoader
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

WATCHED = ("BROWSER_HOST", "BROWSER_CONTROL_PORT", "BROWSER_CDP_PORT", "BROWSER_SSH_PORT", "BROWSER_SCREEN_URL")


def load(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


ai = load("ai_dev_ai_browser", ROOT / "browser" / "ai-browser")


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in WATCHED}
        for key in WATCHED:
            os.environ.pop(key, None)
        self.original = ai.ENV_FILE
        self.tmp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.tmp.name) / "env"
        ai.ENV_FILE = self.env_file  # type: ignore[attr-defined]

    def tearDown(self):
        ai.ENV_FILE = self.original  # type: ignore[attr-defined]
        self.tmp.cleanup()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_reads_values_from_file(self):
        self.env_file.write_text("BROWSER_HOST=box\nBROWSER_CONTROL_PORT=7070\n")
        ai.load_env_file()
        self.assertEqual(ai.host(), "box")
        self.assertEqual(ai.control_port(), 7070)

    def test_empty_export_does_not_shadow_file(self):
        self.env_file.write_text("BROWSER_HOST=box\n")
        os.environ["BROWSER_HOST"] = ""
        ai.load_env_file()
        self.assertEqual(ai.host(), "box")

    def test_existing_value_wins(self):
        self.env_file.write_text("BROWSER_HOST=file-box\n")
        os.environ["BROWSER_HOST"] = "env-box"
        ai.load_env_file()
        self.assertEqual(ai.host(), "env-box")

    def test_missing_file_is_ignored(self):
        ai.load_env_file()  # A missing file must not raise.
        with self.assertRaises(ai.Failure):
            ai.host()


class PortTest(unittest.TestCase):
    def test_defaults(self):
        saved = {k: os.environ.pop(k, None) for k in ("BROWSER_CONTROL_PORT", "BROWSER_CDP_PORT")}
        try:
            self.assertEqual(ai.control_port(), 7070)
            self.assertEqual(ai.cdp_port(), 9223)
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value


class ScreenInstructionsTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
    def test_tunnel_message_keeps_ssh_session_open(self):
        import shlex

        with patch.object(ai, "host", return_value="fixture-browser"), \
             patch.object(ai, "emit", side_effect=lambda value: value), \
             patch.dict(os.environ, {"BROWSER_SSH_PORT": "3022"}):
            command = shlex.split(ai.cmd_screen()["tunnel"])
        self.assertEqual(command[0], "ssh")
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertIn("127.0.0.1:6080:fixture-browser:6080", command)
        process = subprocess.Popen(["sh", "-c", command[-1]], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),
                             "SSH connected. Open the live view to check the tunnel.")
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_screen_points_user_to_container_credentials_without_reading_them(self):
        with tempfile.TemporaryDirectory(prefix="browser screen ") as temp:
            state = Path(temp)
            (state / "screen-auth").write_text("fixture-user:fixture-password", encoding="utf-8")
            with patch.object(ai, "STATE_DIR", state), patch.object(ai, "host", return_value="fixture-browser"), \
                 patch.object(ai, "emit", side_effect=lambda value: value), \
                 patch.object(Path, "read_text", side_effect=AssertionError("must not read credentials")):
                result = ai.cmd_screen()
            self.assertNotIn("fixture-password", str(result))
            self.assertNotIn("username", result)
            if os.name == "posix":
                output = subprocess.check_output(["sh", "-c", result["credential_command"]], text=True)
                self.assertEqual(output, "fixture-user:fixture-password\n")


class BrowserDiagnosticTest(unittest.TestCase):
    def test_missing_address_does_not_claim_browser_is_absent(self):
        with patch.dict(os.environ, {"BROWSER_HOST": ""}):
            with self.assertRaises(ai.Failure) as caught:
                ai.host()
        message = str(caught.exception)
        self.assertIn("not configured", message)
        self.assertNotIn("no browser computer", message)
        self.assertNotIn("scripts/browser.sh", message)

    def test_unreachable_hub_requests_container_and_network_check(self):
        with patch.object(ai, "host", return_value="fixture-browser"), \
             patch.object(ai.urllib.request, "urlopen", side_effect=urllib.error.URLError("fixture")):
            with self.assertRaises(ai.Failure) as caught:
                ai.hub_status()
        message = str(caught.exception)
        self.assertIn("browser container and network", message)
        self.assertNotIn("scripts/browser.sh", message)


class LifecycleTest(unittest.TestCase):
    def test_type_keys_requests_explicit_keystrokes(self):
        with patch.object(ai, "load_env_file"), patch.object(ai, "cmd_actions", return_value=0) as actions:
            self.assertEqual(ai.main(["ai-browser", "type-keys", "中文"]), 0)
        actions.assert_called_once_with([{"type": "type", "text": "中文", "method": "keys"}])

    def test_action_request_timeout_covers_server_action_budget(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        with patch.object(ai, "host", return_value="fixture"), patch.object(ai, "token", return_value="fixture"), \
             patch.object(ai.urllib.request, "urlopen", return_value=response) as call:
            ai.request("POST", "/v1/actions", {"actions": []})
        self.assertGreater(ai.ACTION_TIMEOUT, 180)
        self.assertEqual(call.call_args.kwargs["timeout"], ai.ACTION_TIMEOUT)
    def test_transition_status_is_not_misreported_as_idle_or_awake(self):
        for state in ("starting", "stopping"):
            with self.subTest(state=state), \
                 patch.object(ai, "hub_status", return_value={"ok": True, "state": state, "stack": False}), \
                 patch.object(ai, "host", return_value="fixture"), patch.object(ai, "request") as request, \
                 patch.object(ai, "emit", side_effect=lambda value: value):
                result = ai.cmd_status()
            self.assertEqual(result["state"], state)
            self.assertTrue(result["ok"])
            request.assert_not_called()

    def test_help_succeeds_and_invalid_command_still_fails(self):
        with patch.object(ai, "load_env_file"), patch("builtins.print"):
            self.assertEqual(ai.main(["ai-browser", "--help"]), 0)
            self.assertEqual(ai.main(["ai-browser", "invalid-command"]), 2)

    def test_tabs_ensures_browser_and_reads_once(self):
        pages = [{"id": "one", "url": "https://example.test", "title": "fixture"}]
        with patch.object(ai, "request") as request, patch.object(ai, "pages", return_value=pages) as listing, \
             patch.object(ai, "emit", side_effect=lambda value: value):
            result = ai.cmd_tabs()
        request.assert_called_once_with("POST", "/v1/ensure", {})
        listing.assert_called_once()
        self.assertEqual(result["count"], len(result["tabs"]))

    def test_close_ensures_browser_before_listing(self):
        calls = []
        with patch.object(ai, "request", side_effect=lambda *a: calls.append("ensure")), \
             patch.object(ai, "pages", side_effect=lambda: calls.append("pages") or []), \
             patch.object(ai, "emit", return_value=0):
            ai.cmd_close("all")
        self.assertEqual(calls, ["ensure", "pages"])

    def test_idle_status_does_not_wake(self):
        with patch.object(ai, "hub_status", return_value={"stack": False}), \
             patch.object(ai, "host", return_value="fixture"), patch.object(ai, "request") as request, \
             patch.object(ai, "emit", return_value=0):
            ai.cmd_status()
        request.assert_not_called()

    def test_awake_status_uses_only_passive_hub_information(self):
        hub = {"ok": True, "stack": True, "chromium": True, "display": ":1", "profile": "fixture"}
        with patch.object(ai, "hub_status", return_value=hub) as status, \
             patch.object(ai, "host", return_value="fixture"), patch.object(ai, "request") as request, \
             patch.object(ai, "cdp_json") as cdp, patch.object(ai, "emit", side_effect=lambda value: value):
            result = ai.cmd_status()
        status.assert_called_once()
        request.assert_not_called()
        cdp.assert_not_called()
        self.assertEqual(result["state"], "awake")
        self.assertTrue(result["control"]["chromium"])
        self.assertTrue(result["cdp"]["ok"])

    def test_action_error_payload_survives_cli(self):
        payload = {"ok": False, "completed": 1, "uncertain": True, "error": "fixture failure"}
        with patch.object(ai, "load_env_file"), \
             patch.object(ai, "cmd_actions", side_effect=ai.Failure("error", payload)), \
             patch.object(ai, "emit", return_value=1) as emit:
            self.assertEqual(ai.main(["ai-browser", "type", "hello"]), 1)
        emit.assert_called_once_with(payload, 1)

    def test_transport_failure_marks_action_result_uncertain(self):
        with patch.object(ai, "load_env_file"), \
             patch.object(ai, "cmd_actions", side_effect=ai.Failure("connection lost")), \
             patch.object(ai, "emit", side_effect=lambda value, code: value):
            result = ai.main(["ai-browser", "type", "hello"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["uncertain"])


if __name__ == "__main__":
    unittest.main()
