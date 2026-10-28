"""Unit tests for input validation in browser/control.py."""

import importlib.util
from importlib.machinery import SourceFileLoader
import sys
import subprocess
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


control = load("ai_dev_control", ROOT / "browser" / "control.py")


class AsIntTest(unittest.TestCase):
    def test_accepts_in_range(self):
        self.assertEqual(control.as_int(5, "x", 0, 10), 5)

    def test_rejects_bool(self):
        with self.assertRaises(RuntimeError):
            control.as_int(True, "x", 0, 10)

    def test_rejects_float_and_string(self):
        for value in (1.5, "5", None):
            with self.assertRaises(RuntimeError):
                control.as_int(value, "x", 0, 10)

    def test_rejects_out_of_range(self):
        with self.assertRaises(RuntimeError):
            control.as_int(11, "x", 0, 10)


class AsTextTest(unittest.TestCase):
    def test_accepts_text(self):
        self.assertEqual(control.as_text("hello", "t"), "hello")

    def test_rejects_nul_and_overlong(self):
        with self.assertRaises(RuntimeError):
            control.as_text("a\x00b", "t")
        with self.assertRaises(RuntimeError):
            control.as_text("a" * 5000, "t")

    def test_rejects_non_string(self):
        with self.assertRaises(RuntimeError):
            control.as_text(1, "t")


class KeyTest(unittest.TestCase):
    def test_accepts_common_combos(self):
        for key in ("ctrl+l", "Return", "shift+Tab", "ctrl+shift+t"):
            self.assertTrue(control.KEY_RE.match(key), key)

    def test_rejects_shell_metachars(self):
        for key in ("ctrl+l;rm -rf /", "$(id)", "`id`", "a|b"):
            self.assertIsNone(control.KEY_RE.match(key), key)


class ProcessTest(unittest.TestCase):
    def test_lookup_uses_option_boundary_and_escapes_pattern(self):
        with patch.object(control.subprocess, "run", return_value=Mock(returncode=1)) as run:
            self.assertFalse(control.chromium_running())
        argv = run.call_args.args[0]
        self.assertEqual(argv[4], "--")
        self.assertTrue(argv[5].startswith("\\-\\-user"))

    def test_lookup_failure_does_not_remove_locks(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "SingletonLock"
            lock.write_text("fixture", encoding="utf-8")
            with patch.object(control, "PROFILE", temp), \
                 patch.object(control.subprocess, "run", return_value=Mock(returncode=2)):
                with self.assertRaises(RuntimeError):
                    control.clear_stale_profile_lock()
            self.assertEqual(lock.read_text(), "fixture")

    def test_live_process_keeps_lock_and_does_not_spawn_again(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "SingletonLock"
            lock.write_text("fixture", encoding="utf-8")
            with patch.object(control, "PROFILE", temp), \
                 patch.object(control, "chromium_running", return_value=True), \
                 patch.object(control, "cdp_alive", side_effect=[False, True]), \
                 patch.object(control, "chrome_bin", return_value="chromium"), \
                 patch.object(control, "spawn_logged") as spawn:
                self.assertEqual(control.ensure_chromium(), {"started": False, "cdp": True})
                spawn.assert_not_called()
            self.assertTrue(lock.exists())

    def test_absent_process_allows_stale_lock_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                (Path(temp) / name).touch()
            with patch.object(control, "PROFILE", temp), \
                 patch.object(control, "chromium_running", return_value=False):
                self.assertTrue(control.clear_stale_profile_lock())
            self.assertEqual(list(Path(temp).iterdir()), [])


class ActionTest(unittest.TestCase):
    def test_busy_action_is_rejected_without_queueing(self):
        control.ACTION_LOCK.acquire()
        try:
            with patch.object(control, "xdotool") as run:
                result = control.execute_actions({"actions": [{"type": "key", "key": "Return"}]})
        finally:
            control.ACTION_LOCK.release()
        self.assertFalse(result["ok"])
        self.assertFalse(result["uncertain"])
        self.assertIn("still running", result["error"])
        run.assert_not_called()

    def test_total_action_budget_is_enforced_before_execution(self):
        with patch.object(control, "xdotool") as run, self.assertRaisesRegex(RuntimeError, "budget"):
            control.execute_actions({"actions": [{"type": "key", "key": "Return"}] * 13})
        run.assert_not_called()

    def test_maximum_non_ascii_text_fits_action_budget(self):
        with patch.object(control, "paste_text") as paste:
            result = control.execute_actions({"actions": [{"type": "type", "text": "文" * control.MAX_TEXT}]})
        self.assertTrue(result["ok"])
        self.assertLess(paste.call_args.kwargs["timeout"], control.MAX_ACTION_BUDGET)

    def test_long_text_has_sufficient_budget(self):
        text = "a" * control.MAX_TEXT
        args, budget = control.prepare_action({"type": "type", "text": text})
        self.assertEqual(args[-1], text)
        self.assertGreater(budget, len(text) * 0.012)
        self.assertLessEqual(budget, 60)

    def test_all_actions_are_validated_before_execution(self):
        body = {"actions": [{"type": "click", "x": 1, "y": 1},
                            {"type": "type", "text": "a" * 4097}]}
        with patch.object(control, "xdotool") as tool, self.assertRaises(RuntimeError):
            control.execute_actions(body)
        tool.assert_not_called()

    def test_invalid_settle_is_rejected_before_execution(self):
        with patch.object(control, "xdotool") as tool, self.assertRaises(RuntimeError):
            control.execute_actions({"actions": [{"type": "key", "key": "Return"}], "settleMs": -1})
        tool.assert_not_called()

    def test_mid_batch_failure_reports_partial_progress(self):
        actions = [{"type": "key", "key": "Return"}, {"type": "type", "text": "hello"}]
        with patch.object(control, "xdotool", side_effect=[None, subprocess.TimeoutExpired("fixture", 15)]):
            result = control.execute_actions({"actions": actions})
        self.assertFalse(result["ok"])
        self.assertEqual(result["completed"], 1)
        self.assertTrue(result["uncertain"])

    def test_success_reports_settled_progress(self):
        with patch.object(control, "xdotool"):
            result = control.execute_actions({"actions": [{"type": "type", "text": "hello"}]})
        self.assertEqual(result, {"ok": True, "completed": 1, "uncertain": False})


class TypeDelayTest(unittest.TestCase):
    def test_non_ascii_text_uses_clipboard(self):
        args, _budget = control.prepare_action({"type": "type", "text": "中文"})
        self.assertEqual(args, ["__clipboard__", "中文"])

    def test_ascii_text_keeps_the_fast_delay(self):
        args, _budget = control.prepare_action({"type": "type", "text": "hello"})
        self.assertEqual(int(args[args.index("--delay") + 1]), 12)

    def test_explicit_key_typing_preserves_old_delay_and_budget(self):
        text = "中" * 100
        args, budget = control.prepare_action({"type": "type", "text": text, "method": "keys"})
        delay = int(args[args.index("--delay") + 1])
        self.assertEqual(delay, 40)
        self.assertGreaterEqual(budget, len(text) * delay / 1000)

    def test_invalid_method_is_rejected_before_batch_execution(self):
        with patch.object(control, "xdotool") as tool, patch.object(control, "paste_text") as paste:
            with self.assertRaisesRegex(RuntimeError, "method"):
                control.execute_actions({"actions": [{"type": "key", "key": "Return"},
                                                    {"type": "type", "text": "中文", "method": "bad"}]})
        tool.assert_not_called()
        paste.assert_not_called()


class ClipboardPasteTest(unittest.TestCase):
    def test_write_readback_and_paste_use_utf8_without_output_pipes(self):
        text = "中文🙂\nsecond line"
        results = [Mock(returncode=0), Mock(returncode=0, stdout=text.encode())]
        with patch.object(control.subprocess, "run", side_effect=results) as run, \
             patch.object(control, "xdotool") as key, patch.object(control.time, "sleep"):
            control.paste_text(text, timeout=15)
        write, read = run.call_args_list
        self.assertEqual(write.kwargs["input"], text.encode())
        self.assertEqual(write.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(write.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(read.args[0][-1], "-out")
        key.assert_called_once()
        self.assertEqual(key.call_args.args, ("key", "--clearmodifiers", "ctrl+v"))
        self.assertGreater(key.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(key.call_args.kwargs["timeout"], 15)

    def test_readback_retries_without_pasting_old_clipboard(self):
        results = [Mock(returncode=0), Mock(returncode=0, stdout=b"old"),
                   Mock(returncode=0, stdout="中文".encode())]
        with patch.object(control.subprocess, "run", side_effect=results) as run, \
             patch.object(control, "xdotool") as key, patch.object(control.time, "sleep"):
            control.paste_text("中文", timeout=15)
        self.assertEqual(run.call_count, 3)
        key.assert_called_once()

    def test_timed_out_readback_can_retry_before_deadline(self):
        results = [Mock(returncode=0), subprocess.TimeoutExpired("xclip", 0.5),
                   Mock(returncode=0, stdout="中文".encode())]
        with patch.object(control.subprocess, "run", side_effect=results), \
             patch.object(control, "xdotool") as key, patch.object(control.time, "sleep"):
            control.paste_text("中文", timeout=15)
        key.assert_called_once()

    def test_write_timeout_does_not_send_paste(self):
        with patch.object(control.subprocess, "run", side_effect=subprocess.TimeoutExpired("xclip", 5)), \
             patch.object(control, "xdotool") as key:
            with self.assertRaisesRegex(RuntimeError, "clipboard write timed out"):
                control.paste_text("中文", timeout=15)
        key.assert_not_called()

    def test_write_failure_does_not_send_paste_or_expose_text(self):
        with patch.object(control.subprocess, "run", return_value=Mock(returncode=1)), \
             patch.object(control, "xdotool") as key:
            result = control.execute_actions({"actions": [{"type": "type", "text": "中文fixture"}]})
        self.assertFalse(result["ok"])
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["completed"], 0)
        self.assertNotIn("中文fixture", result["error"])
        key.assert_not_called()

    def test_readback_deadline_never_sends_paste(self):
        results = [Mock(returncode=0)] + [Mock(returncode=0, stdout=b"old")] * 20
        with patch.object(control.subprocess, "run", side_effect=results), \
             patch.object(control.time, "monotonic", side_effect=range(100)), \
             patch.object(control.time, "sleep"), patch.object(control, "xdotool") as key:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                control.paste_text("中文", timeout=15)
        key.assert_not_called()

    def test_batch_continues_after_paste_and_stops_on_failure(self):
        actions = [{"type": "type", "text": "中文"}, {"type": "key", "key": "Return"}]
        with patch.object(control, "paste_text") as paste, patch.object(control, "xdotool") as key:
            result = control.execute_actions({"actions": actions})
        self.assertEqual(result["completed"], 2)
        paste.assert_called_once()
        key.assert_called_once()
        with patch.object(control, "paste_text", side_effect=RuntimeError("clipboard write failed")), \
             patch.object(control, "xdotool") as key:
            result = control.execute_actions({"actions": actions})
        self.assertFalse(result["ok"])
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["completed"], 0)
        key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
