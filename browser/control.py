#!/usr/bin/env python3
"""Token-authenticated desktop control for the ai-dev-env browser computer.

The agent-facing CLI talks to this over the per-user Docker network:

    GET  /v1/health         liveness + Chromium/CDP state (no token)
    POST /v1/ensure         start Chromium if needed
    GET  /v1/screenshot     PNG of the whole display
    POST /v1/actions        ordered input actions (click / type / key / ...)

Input targets the same desktop a human sees in noVNC. Non-ASCII text uses
the X clipboard and a simulated paste; explicit key typing remains available.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai_dev_logs import spawn_logged

TOKEN = os.environ.get("BROWSER_CONTROL_TOKEN", "")
DISPLAY = os.environ.get("DISPLAY", ":1")
PROFILE = os.environ.get("BROWSER_PROFILE", "/home/dev/.ai-data/profile")
CDP_PORT = int(os.environ.get("BROWSER_CDP_PORT", "9222"))
LOG_DIR = "/tmp/browser"
MAX_BODY_BYTES = 64 * 1024
MAX_ACTIONS = 32
MAX_TEXT = 4096
SETTLE_MAX_MS = 5000
MAX_ACTION_BUDGET = 180.0

CHROME_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "brave-browser",
)

CHROME_FLAGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--enable-unsafe-swiftshader",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "--password-store=basic",
    "--restore-last-session",
    "--start-maximized",
    "--remote-debugging-address=127.0.0.1",
    f"--remote-debugging-port={CDP_PORT}",
    f"--user-data-dir={PROFILE}",
]

LAUNCH_LOCK = threading.Lock()
ACTION_LOCK = threading.Lock()


def chrome_bin() -> str | None:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if not path:
            continue
        # Ubuntu ships a snap wrapper that cannot run these flags.
        try:
            with open(path, "rb") as handle:
                head = handle.read(4096)
        except OSError:
            continue
        if head.startswith(b"#!") and b"/snap/" in head:
            continue
        return path
    return None


def cdp_alive(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def chromium_running() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", "--", re.escape(f"--user-data-dir={PROFILE}")],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("cannot determine whether Chromium is running") from error
    if out.returncode not in (0, 1):
        raise RuntimeError("Chromium process lookup failed")
    return out.returncode == 0


def clear_stale_profile_lock() -> bool:
    if chromium_running():
        return False
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.unlink(os.path.join(PROFILE, name))
        except OSError:
            pass
    return True


def ensure_chromium(timeout_s: float = 20.0) -> dict:
    with LAUNCH_LOCK:
        if cdp_alive():
            return {"started": False, "cdp": True}
        binary = chrome_bin()
        if not binary:
            raise RuntimeError("no Chromium/Chrome binary in this image")
        started = clear_stale_profile_lock()
        if started:
            env = {**os.environ, "DISPLAY": DISPLAY}
            env.setdefault("HOME", "/home/dev")
            os.makedirs(LOG_DIR, exist_ok=True)
            spawn_logged([binary, *CHROME_FLAGS], os.path.join(LOG_DIR, "chromium.log"), env)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if cdp_alive():
                return {"started": started, "cdp": True}
            time.sleep(0.25)
        raise RuntimeError(f"Chromium did not expose CDP on 127.0.0.1:{CDP_PORT}")


def xdotool(*args: str, timeout: float = 15) -> None:
    result = subprocess.run(
        ["xdotool", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "DISPLAY": DISPLAY},
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"xdotool {args[0]} failed")


def paste_text(text: str, timeout: float) -> None:
    """Set and verify the X clipboard, then send paste within one deadline."""
    deadline = time.monotonic() + timeout
    env = {**os.environ, "DISPLAY": DISPLAY}
    payload = text.encode("utf-8")

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise RuntimeError("clipboard paste timed out")
        return seconds

    # xclip forks a selection owner. Capturing its stdout/stderr in pipes can
    # keep communicate() waiting after the parent has exited.
    try:
        write = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "UTF8_STRING", "-in"],
            input=payload, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=min(5.0, remaining()), env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("clipboard write timed out") from error
    if write.returncode:
        raise RuntimeError("clipboard write failed")

    while True:
        try:
            read = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "UTF8_STRING", "-out"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=min(0.5, remaining()), env=env,
            )
        except subprocess.TimeoutExpired:
            read = None
        if read is not None and read.returncode == 0 and read.stdout == payload:
            break
        time.sleep(min(0.05, remaining()))

    xdotool("key", "--clearmodifiers", "ctrl+v", timeout=remaining())
    # This allows the focused application to process the event; it does not
    # prove that the application accepted the paste.
    time.sleep(min(0.2, remaining()))
    remaining()


def as_int(value, name: str, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"{name} must be an integer")
    if value < low or value > high:
        raise RuntimeError(f"{name} out of range ({low}..{high})")
    return value


def as_text(value, name: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{name} must be a string")
    if len(value) > limit:
        raise RuntimeError(f"{name} longer than {limit} characters")
    if "\x00" in value:
        raise RuntimeError(f"{name} contains NUL")
    return value


KEY_RE = re.compile(r"^[A-Za-z0-9_+\-\[\]]{1,64}$")


def prepare_action(action: dict) -> tuple[list[str] | None, float]:
    """Validate without changing the desktop; return command and time budget."""
    if not isinstance(action, dict):
        raise RuntimeError("action must be an object")
    kind = action.get("type")
    if kind == "click":
        x, y = as_int(action.get("x"), "x", 0, 16384), as_int(action.get("y"), "y", 0, 16384)
        button = as_int(action.get("button", 1), "button", 1, 9)
        count = as_int(action.get("count", 1), "count", 1, 3)
        args = ["mousemove", str(x), str(y), "click", "--repeat", str(count), str(button)]
    elif kind == "move":
        x, y = as_int(action.get("x"), "x", 0, 16384), as_int(action.get("y"), "y", 0, 16384)
        args = ["mousemove", str(x), str(y)]
    elif kind == "type":
        text = as_text(action.get("text"), "text")
        method = action.get("method", "auto")
        if method not in ("auto", "keys"):
            raise RuntimeError("type method must be auto or keys")
        if method == "auto" and not text.isascii():
            return ["__clipboard__", text], 15.0
        delay = 12 if text.isascii() else 40
        return ["type", "--clearmodifiers", "--delay", str(delay), "--", text], \
            max(15.0, len(text) * delay / 1000 + 10)
    elif kind == "key":
        key = as_text(action.get("key"), "key", 64)
        if not KEY_RE.match(key):
            raise RuntimeError("unsupported key combination")
        args = ["key", "--clearmodifiers", key]
    elif kind == "scroll":
        x, y = as_int(action.get("x", 640), "x", 0, 16384), as_int(action.get("y", 400), "y", 0, 16384)
        steps = as_int(action.get("steps", 3), "steps", 1, 50)
        direction = action.get("direction", "down")
        if direction not in ("up", "down", "left", "right"):
            raise RuntimeError("direction must be up/down/left/right")
        button = {"up": "4", "down": "5", "left": "6", "right": "7"}[direction]
        args = ["mousemove", str(x), str(y), "click", "--repeat", str(steps), button]
    elif kind == "drag":
        x1, y1 = as_int(action.get("x1"), "x1", 0, 16384), as_int(action.get("y1"), "y1", 0, 16384)
        x2, y2 = as_int(action.get("x2"), "x2", 0, 16384), as_int(action.get("y2"), "y2", 0, 16384)
        args = [
            "mousemove", str(x1), str(y1), "mousedown", "1",
            "mousemove", str(x2), str(y2), "mouseup", "1",
        ]
    elif kind == "wait":
        return None, as_int(action.get("ms", 200), "ms", 0, SETTLE_MAX_MS) / 1000
    else:
        raise RuntimeError(f"unsupported action type: {kind!r}")
    return args, 15.0


def execute_actions(body: dict) -> dict:
    actions = body.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
        raise RuntimeError(f"actions must contain 1 to {MAX_ACTIONS} items")
    prepared = [prepare_action(action) for action in actions]
    settle = as_int(body.get("settleMs", 0), "settleMs", 0, SETTLE_MAX_MS)
    if sum(budget for _, budget in prepared) + settle / 1000 > MAX_ACTION_BUDGET:
        raise RuntimeError("action batch exceeds the 180 second execution budget")
    completed = 0
    uncertain = False
    if not ACTION_LOCK.acquire(blocking=False):
        return {"ok": False, "completed": 0, "uncertain": False, "error": "another action is still running"}
    try:
        for args, budget in prepared:
            uncertain = args is not None
            if args is None:
                time.sleep(budget)
            elif args[0] == "__clipboard__":
                paste_text(args[1], timeout=budget)
            else:
                xdotool(*args, timeout=budget)
            completed += 1
            uncertain = False
        if settle:
            time.sleep(settle / 1000)
        return {"ok": True, "completed": completed, "uncertain": False}
    except Exception as error:  # noqa: BLE001 - report partial desktop changes
        return {"ok": False, "completed": completed, "uncertain": uncertain, "error": str(error)}
    finally:
        ACTION_LOCK.release()


def screenshot() -> bytes:
    result = subprocess.run(
        ["import", "-display", DISPLAY, "-window", "root", "png:-"],
        capture_output=True,
        timeout=30,
        env={**os.environ, "DISPLAY": DISPLAY},
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(result.stderr.decode("utf-8", "replace") or "screen capture failed")
    return result.stdout


def authorized(header: str | None) -> bool:
    if not TOKEN or not header:
        return False
    supplied = header.removeprefix("Bearer ").strip()
    return hmac.compare_digest(supplied, TOKEN)


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-dev-browser-control/1"

    def log_message(self, format, *args):  # noqa: A002 - silence default logging
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/v1/health":
            self._json(200, {
                "ok": True,
                "display": DISPLAY,
                "chromium": cdp_alive(),
                "profile": PROFILE,
            })
            return
        if self.path == "/v1/screenshot":
            if not authorized(self.headers.get("Authorization")):
                self._json(401, {"error": "unauthorized"})
                return
            try:
                self._send(200, screenshot(), "image/png")
            except Exception as error:  # noqa: BLE001
                self._json(500, {"error": str(error)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not authorized(self.headers.get("Authorization")):
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY_BYTES:
                raise RuntimeError("request body too large")
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise RuntimeError("body must be an object")

            if self.path == "/v1/ensure":
                self._json(200, ensure_chromium())
                return
            if self.path == "/v1/actions":
                result = execute_actions(body)
                self._json(200 if result["ok"] else 500, result)
                return
            self._json(404, {"error": "not found"})
        except Exception as error:  # noqa: BLE001
            self._json(400 if self.path == "/v1/actions" else 500,
                       {"ok": False, "completed": 0, "uncertain": False, "error": str(error)})


def main() -> int:
    if not TOKEN:
        raise SystemExit("BROWSER_CONTROL_TOKEN is required")
    port = int(os.environ.get("BROWSER_CONTROL_PORT", "7071"))
    # Loopback only: the hub proxies the public 7070 port.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
