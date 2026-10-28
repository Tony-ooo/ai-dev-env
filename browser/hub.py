#!/usr/bin/env python3
"""Browser computer hub.

Keeps a small listener alive inside the container and starts the heavy desktop
stack (TigerVNC / fluxbox / noVNC / Chromium) only when someone connects.
After BROWSER_IDLE_SECONDS without connections the stack is stopped again,
leaving only the lightweight hub resident.

Public ports (container network):
  6080  noVNC          -> websockify 127.0.0.1:6081
  7070  control API    -> control   127.0.0.1:7071   (also serves /__hub/status)
  9223  CDP gate       -> chromium  127.0.0.1:9222   (Host header rewritten)
"""

from __future__ import annotations

import json
import base64
import binascii
import hmac
import secrets
from urllib.parse import parse_qs, urlsplit
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

from ai_dev_logs import spawn_logged

DISPLAY = os.environ.get("DISPLAY", ":1")
HOME = os.environ.get("HOME", "/home/dev")
STATE_DIR = os.path.join(HOME, ".ai-data", "state")
PROFILE_DIR = os.path.join(HOME, ".ai-data", "profile")
AUTH_FILE = os.path.join(STATE_DIR, "screen-auth")
LOG_DIR = "/tmp/browser"
IDLE_SECONDS = int(os.environ.get("BROWSER_IDLE_SECONDS", "600"))
CONTROL_TOKEN = os.environ.get("BROWSER_CONTROL_TOKEN", "")

# internal ports (loopback only)
WEBSOCKIFY_PORT = 6081
CONTROL_PORT = 7071
CHROMIUM_PORT = 9222
VNC_PORT = 5900

PUBLIC = (
    (6080, WEBSOCKIFY_PORT, False),
    (7070, CONTROL_PORT, False),
    (9223, CHROMIUM_PORT, True),
)

LOCK = threading.RLock()
PROCS: dict[str, subprocess.Popen] = {}
READY = False
TRANSITION = ""
ACTIVE = 0
LAST_ACTIVE = time.time()
STOPPING = threading.Event()
LAST_ERROR = ""
NEXT_START = 0.0
REQUIRED_PROCS = ("vnc", "vncconfig", "fluxbox", "websockify", "control")
MAX_HEADER_BYTES = 64 * 1024
SCREEN_SESSIONS: dict[str, tuple[float, bytes]] = {}
SCREEN_SESSION_LOCK = threading.Lock()
SCREEN_SESSION_KEY = os.urandom(32)
SCREEN_SESSION_SECONDS = 3600


def log(message: str) -> None:
    print(f"[hub] {message}", flush=True)


def ensure_dirs() -> None:
    for path in (LOG_DIR, "/tmp/.X11-unix", "/tmp/fluxbox-home",
                 os.path.join(HOME, ".ai-data", "profile"),
                 os.path.join(HOME, ".ai-data", "downloads"),
                 STATE_DIR):
        os.makedirs(path, exist_ok=True)


def screen_auth() -> str:
    """websockify compares the `user:pass` string itself, not a file path."""
    try:
        with open(AUTH_FILE, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        value = ""
    if value:
        return value
    import base64
    os.makedirs(STATE_DIR, exist_ok=True)
    value = "dev:" + base64.b64encode(os.urandom(14)).decode().replace("=", "").replace("+", "").replace("/", "")
    fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
    log(f"generated {AUTH_FILE}")
    return value


def spawn(name: str, argv: list[str], env: dict | None = None) -> subprocess.Popen:
    log_path = os.path.join(LOG_DIR, f"{name}.log")
    proc = spawn_logged(argv, log_path, env=env or dict(os.environ))
    PROCS[name] = proc
    return proc


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def vnc_ready(timeout: float = 1.0) -> bool:
    """Complete an RFB handshake so health probes are not failed logins."""
    try:
        with socket.create_connection(("127.0.0.1", VNC_PORT), timeout=timeout) as conn:
            conn.settimeout(timeout)

            def read_exact(size: int) -> bytes:
                data = bytearray()
                while len(data) < size:
                    chunk = conn.recv(size - len(data))
                    if not chunk:
                        raise OSError("VNC handshake closed")
                    data.extend(chunk)
                return bytes(data)

            if read_exact(12) != b"RFB 003.008\n":
                return False
            conn.sendall(b"RFB 003.008\n")
            if 1 not in read_exact(read_exact(1)[0]):
                return False
            conn.sendall(b"\x01")
            if read_exact(4) != b"\x00" * 4:
                return False
            conn.sendall(b"\x01")  # Shared client; do not disconnect the viewer.
            header = read_exact(24)
            name_length = struct.unpack(">I", header[20:24])[0]
            if name_length > 65536:
                return False
            read_exact(name_length)
            return True
    except (OSError, ValueError):
        return False


def wait_port(port: int, timeout: float, name: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (vnc_ready() if name == "vnc" else port_open(port)):
            return
        proc = PROCS.get(name)
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"{name} exited with {proc.returncode}; see {LOG_DIR}/{name}.log")
        time.sleep(0.1)
    raise RuntimeError(f"{name} did not listen on {port} within {timeout:.0f}s")


def dbus_env() -> dict:
    env = dict(os.environ)
    try:
        out = subprocess.run(
            ["dbus-launch", "--sh-syntax"], capture_output=True, text=True, timeout=5
        )
        # Output is a shell fragment:
        #   DBUS_SESSION_BUS_ADDRESS='unix:path=...';
        #   export DBUS_SESSION_BUS_ADDRESS;
        #   DBUS_SESSION_BUS_PID=1234;
        # Only the assignment carries the value; `export KEY;` has none.
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("export ") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.rstrip(";").strip().strip("'\"")
            if key.startswith("DBUS_") and value:
                env[key] = value
    except Exception:  # noqa: BLE001 - dbus is optional
        pass
    return env


def copy_fluxbox_config() -> None:
    target = "/tmp/fluxbox-home/.fluxbox"
    os.makedirs(target, exist_ok=True)
    for name in ("init", "apps", "menu"):
        src = os.path.join("/etc/ai-dev/fluxbox", name)
        if os.path.exists(src):
            with open(src, "rb") as handle:
                data = handle.read()
            with open(os.path.join(target, name), "wb") as handle:
                handle.write(data)


def start_stack() -> None:
    """Start every desktop component. Caller must hold LOCK."""
    global READY, TRANSITION
    if READY:
        return
    TRANSITION = "starting"
    ensure_dirs()
    log("starting desktop stack")

    try:
        os.unlink("/tmp/.X1-lock")
    except OSError:
        pass

    # TigerVNC supports the Unicode clipboard extension used by noVNC. The
    # server remains private to loopback; websockify provides screen auth.
    spawn("vnc", ["Xtigervnc", DISPLAY, "-geometry", "1280x800", "-depth", "24",
                  "-localhost", "-rfbport", str(VNC_PORT), "-SecurityTypes", "None",
                  "-AlwaysShared", "-ac", "-nolisten", "tcp"])
    wait_port(VNC_PORT, 10, "vnc")
    deadline = time.time() + 10
    while time.time() < deadline:
        probe = subprocess.run(["xdpyinfo", "-display", DISPLAY],
                                capture_output=True, timeout=5)
        if probe.returncode == 0:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("TigerVNC display did not become ready")

    env = dbus_env()
    spawn("vncconfig", ["vncconfig", "-nowin"], env={**env, "DISPLAY": DISPLAY})
    copy_fluxbox_config()
    spawn("fluxbox", ["fluxbox", "-rc", "/tmp/fluxbox-home/.fluxbox/init"],
          env={**env, "HOME": "/tmp/fluxbox-home"})
    # FileAuth reads the password from AUTH_FILE, so `ps` only shows a path.
    spawn("websockify", ["websockify", "--web=/usr/share/novnc",
                         "--auth-plugin=ai_dev_auth.FileAuth",
                         f"--auth-source={AUTH_FILE}",
                         f"127.0.0.1:{WEBSOCKIFY_PORT}", f"127.0.0.1:{VNC_PORT}"])
    wait_port(WEBSOCKIFY_PORT, 10, "websockify")

    control_env = {**env, "DISPLAY": DISPLAY, "HOME": HOME,
                   "BROWSER_CONTROL_PORT": str(CONTROL_PORT),
                   "BROWSER_CONTROL_TOKEN": CONTROL_TOKEN}
    spawn("control", [sys.executable, "/usr/local/bin/browser-control"], env=control_env)
    wait_port(CONTROL_PORT, 15, "control")

    READY = True
    TRANSITION = ""
    log("desktop stack ready")


def reap_orphans() -> None:
    """PID 1 must reap adopted children (Chromium helpers, dbus) or they linger as zombies."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def chromium_alive() -> bool:
    """Exited children may remain zombies until their owner reaps them."""
    result = subprocess.run(
        ["pgrep", "-u", str(os.getuid()), "-r", "R,S,D,T,t,I", "-f", "chromium"],
        capture_output=True, timeout=3,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("Chromium shutdown process lookup failed")
    return result.returncode == 0


def stop_chromium() -> None:
    # Keep control's stdout consumer and the display alive until Chromium exits.
    uid = str(os.getuid())
    subprocess.run(["pkill", "-TERM", "-u", uid, "-f", "chromium"], capture_output=True, timeout=3)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if not chromium_alive():
            break
        time.sleep(0.2)
    if chromium_alive():
        log("chromium did not exit on SIGTERM; killing")
        subprocess.run(["pkill", "-KILL", "-u", uid, "-f", "chromium"], capture_output=True, timeout=3)
        deadline = time.monotonic() + 3
        while chromium_alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        if chromium_alive():
            raise RuntimeError("Chromium is still running; profile locks retained")
    subprocess.run(["pkill", "-KILL", "-u", uid, "-f", "chrome_crashpad"], capture_output=True, timeout=3)


def stop_stack() -> None:
    """Stop every desktop component, keep the hub listening. Caller holds LOCK."""
    global READY, TRANSITION
    if not READY and not PROCS:
        TRANSITION = ""
        return
    TRANSITION = "stopping"
    READY = False
    log("stopping desktop stack")
    try:
        stop_chromium()
        processes = list(reversed(PROCS.items()))
        for _, proc in processes:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + 5
        for _, proc in processes:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(timeout=3)
        PROCS.clear()
        subprocess.run(["pkill", "-TERM", "-u", str(os.getuid()), "-x", "dbus-daemon"],
                       capture_output=True, timeout=3)
        # Clear singleton locks only after all live Chromium processes are gone.
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.unlink(os.path.join(PROFILE_DIR, name))
            except OSError:
                pass
        reap_orphans()
        try:
            os.unlink("/tmp/.X1-lock")
        except OSError:
            pass
        log("desktop stack stopped")
    finally:
        TRANSITION = ""


def ensure_ready() -> bool:
    global LAST_ERROR, NEXT_START
    with LOCK:
        if READY and stack_healthy():
            return True
        if time.monotonic() < NEXT_START:
            return False
        try:
            if READY or PROCS:
                stop_stack()
            start_stack()
            LAST_ERROR = ""
            return True
        except Exception as error:  # noqa: BLE001
            LAST_ERROR = str(error)
            log(f"start failed: {LAST_ERROR}")
            try:
                stop_stack()
            except Exception as cleanup_error:  # noqa: BLE001
                LAST_ERROR += f"; cleanup failed: {cleanup_error}"
            NEXT_START = time.monotonic() + 5
            return False


def stack_healthy() -> bool:
    return (all(name in PROCS and PROCS[name].poll() is None for name in REQUIRED_PROCS)
            and vnc_ready()
            and all(port_open(port) for port in (WEBSOCKIFY_PORT, CONTROL_PORT)))


def reaper() -> None:
    global LAST_ERROR, NEXT_START
    while not STOPPING.wait(5):
        with LOCK:
            try:
                if READY and not stack_healthy():
                    log("desktop component failed; resetting stack")
                    stop_stack()
                    LAST_ERROR = "desktop component exited; the next operation restarts it"
                elif READY and ACTIVE == 0 and time.time() - LAST_ACTIVE > IDLE_SECONDS:
                    stop_stack()
            except Exception as error:  # noqa: BLE001
                LAST_ERROR = f"desktop shutdown failed: {error}"
                NEXT_START = time.monotonic() + 5
                log(LAST_ERROR)
            if not READY:
                reap_orphans()


def rewrite_host(first: bytes) -> bytes:
    head, sep, rest = first.partition(b"\r\n\r\n")
    if not sep:
        return first
    lines = head.split(b"\r\n")
    kept = [lines[0]]
    for line in lines[1:]:
        if line.lower().startswith(b"host:"):
            continue
        kept.append(line)
    kept.append(f"Host: 127.0.0.1:{CHROMIUM_PORT}".encode())
    return b"\r\n".join(kept) + sep + rest


def read_request_head(client: socket.socket, timeout: float = 15) -> bytes:
    """Read one bounded HTTP head without discarding coalesced body data."""
    data = bytearray()
    deadline = time.monotonic() + timeout
    while b"\r\n\r\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request header timed out")
        client.settimeout(remaining)
        chunk = client.recv(min(4096, MAX_HEADER_BYTES + 1 - len(data)))
        if not chunk:
            raise ValueError("incomplete request header")
        data.extend(chunk)
        end = data.find(b"\r\n\r\n")
        if (end + 4 if end >= 0 else len(data)) > MAX_HEADER_BYTES:
            raise ValueError("request header too large")
    return bytes(data)


def screen_auth_bytes() -> bytes:
    try:
        with open(AUTH_FILE, "rb") as handle:
            return handle.read(4096).strip()
    except OSError:
        return b""


def request_headers(request: bytes) -> dict[bytes, list[bytes]]:
    head = request.split(b"\r\n\r\n", 1)[0]
    headers: dict[bytes, list[bytes]] = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            name, value = line.split(b":", 1)
            headers.setdefault(name.lower(), []).append(value.strip())
    return headers


def screen_authorized(request: bytes) -> bool:
    values = request_headers(request).get(b"authorization", [])
    if len(values) != 1:
        return False
    scheme, separator, encoded = values[0].partition(b" ")
    if not separator or scheme.lower() != b"basic":
        return False
    try:
        supplied = base64.b64decode(encoded, validate=True)
        expected = screen_auth_bytes()
        return bool(expected) and hmac.compare_digest(supplied, expected)
    except (OSError, ValueError, binascii.Error):
        return False


def screen_session_authorized(request: bytes) -> bool:
    headers = request_headers(request)
    cookies = headers.get(b"cookie", [])
    tokens = [part.strip()[len(b"ai_dev_screen="):] for value in cookies for part in value.split(b";")
              if part.strip().startswith(b"ai_dev_screen=")]
    if len(tokens) != 1:
        return False
    try:
        token = tokens[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    with SCREEN_SESSION_LOCK:
        session = SCREEN_SESSIONS.get(token)
        if not session or session[0] <= time.monotonic():
            SCREEN_SESSIONS.pop(token, None)
            return False
    expected = screen_auth_bytes()
    return bool(expected) and hmac.compare_digest(
        session[1], hmac.digest(SCREEN_SESSION_KEY, expected, "sha256"))


def screen_login_page() -> bytes:
    body = ('''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>浏览器桌面登录</title><style>body{font:16px sans-serif;
            background:#202124;color:#eee;display:grid;place-items:center;min-height:90vh}
            form{display:grid;gap:12px;width:min(320px,85vw)}input,button{font:inherit;
            padding:10px;border-radius:6px;border:1px solid #888}</style>
            <form method="post" action="/__screen/login"><h1>浏览器桌面</h1>
            <p>请输入浏览器画面的账号和密码。</p>
            <label>账号 <input name="username" autocomplete="username" required></label>
            <label>密码 <input name="password" type="password" autocomplete="current-password" required></label>
            <button type="submit">连接</button></form></html>''').encode("utf-8")
    return (b"HTTP/1.1 401 Unauthorized\r\nContent-Type: text/html; charset=utf-8\r\n"
            b"Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
            b"Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)


def screen_login(client: socket.socket, request: bytes) -> bytes:
    headers = request_headers(request)
    lengths = headers.get(b"content-length", [])
    origins, hosts = headers.get(b"origin", []), headers.get(b"host", [])
    if len(lengths) != 1 or len(origins) != 1 or len(hosts) != 1:
        return screen_login_page()
    try:
        origin = urlsplit(origins[0].decode("ascii"))
        if origin.scheme not in ("http", "https") or origin.netloc.encode("ascii").lower() != hosts[0].lower():
            return screen_login_page()
        size = int(lengths[0])
        if not 0 < size <= 4096:
            return screen_login_page()
        body = bytearray(request.split(b"\r\n\r\n", 1)[1])
        client.settimeout(10)
        while len(body) < size:
            chunk = client.recv(size - len(body))
            if not chunk:
                return screen_login_page()
            body.extend(chunk)
        values = parse_qs(bytes(body[:size]).decode("utf-8"), strict_parsing=True, max_num_fields=2)
        username, password = values.get("username", []), values.get("password", [])
        if len(username) != 1 or len(password) != 1:
            return screen_login_page()
        supplied = (username[0] + ":" + password[0]).encode("utf-8")
        expected = screen_auth_bytes()
        if not expected or not hmac.compare_digest(supplied, expected):
            return screen_login_page()
    except (ValueError, UnicodeError, OSError):
        return screen_login_page()
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with SCREEN_SESSION_LOCK:
        for old, session in list(SCREEN_SESSIONS.items()):
            if session[0] <= now:
                del SCREEN_SESSIONS[old]
        if len(SCREEN_SESSIONS) >= 64:
            del SCREEN_SESSIONS[min(SCREEN_SESSIONS, key=lambda key: SCREEN_SESSIONS[key][0])]
        SCREEN_SESSIONS[token] = (now + SCREEN_SESSION_SECONDS, hmac.digest(SCREEN_SESSION_KEY, expected, "sha256"))
    return (b"HTTP/1.1 303 See Other\r\nLocation: /vnc.html?autoconnect=true&resize=scale&reconnect=true\r\n"
            b"Cache-Control: no-store\r\nSet-Cookie: ai_dev_screen=" + token.encode("ascii") +
            b"; Path=/; HttpOnly; SameSite=Strict; Max-Age=3600\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


def add_screen_basic_auth(request: bytes) -> bytes:
    expected = screen_auth_bytes()
    if not expected:
        raise ValueError("screen authentication unavailable")
    head, separator, body = request.partition(b"\r\n\r\n")
    lines = [line for line in head.split(b"\r\n") if not line.lower().startswith(b"authorization:")]
    lines.append(b"Authorization: Basic " + base64.b64encode(expected))
    return b"\r\n".join(lines) + separator + body


def pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def hub_status() -> bytes:
    # Lifecycle changes hold LOCK while waiting for processes. Status must not
    # wait for those operations or its caller can time out during normal idle.
    acquired = LOCK.acquire(timeout=0.05)
    try:
        transition = TRANSITION
        healthy = True if transition else (stack_healthy() if acquired and READY else not LAST_ERROR)
        state = transition or (("awake" if READY else "idle") if healthy else "degraded")
        body = json.dumps({
            "ok": healthy,
            "hub": True,
            "stack": READY,
            "chromium": READY and not transition and acquired and port_open(CHROMIUM_PORT),
            "profile": PROFILE_DIR,
            "state": state,
            "error": LAST_ERROR if not healthy else "",
            "active_connections": ACTIVE,
            "idle_seconds": int(time.time() - LAST_ACTIVE),
            "idle_timeout": IDLE_SECONDS,
            "display": DISPLAY,
        }).encode()
    finally:
        if acquired:
            LOCK.release()
    status = b"200 OK" if healthy else b"503 Service Unavailable"
    return (b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)


def handle(client: socket.socket, backend: int, rewrite: bool, offer_status: bool) -> None:
    global ACTIVE, LAST_ACTIVE
    try:
        first = read_request_head(client)
    except (OSError, ValueError):
        try:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        finally:
            client.close()
        return

    # Hub health must not count as activity, or the Docker healthcheck would
    # keep the desktop awake forever.
    if offer_status and first.split(b"\r\n", 1)[0].split()[:2] == [b"GET", b"/__hub/status"]:
        try:
            client.sendall(hub_status())
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass
        return

    screen_session = False
    if backend == WEBSOCKIFY_PORT:
        parts = first.split(b"\r\n", 1)[0].split()
        if parts[:2] == [b"POST", b"/__screen/login"]:
            try:
                client.sendall(screen_login(client, first))
            except OSError:
                pass
            finally:
                client.close()
            return
        screen_session = screen_session_authorized(first)
    if backend == WEBSOCKIFY_PORT and not (screen_session or screen_authorized(first)):
        try:
            client.sendall(screen_login_page())
        except OSError:
            pass
        finally:
            client.close()
        return

    with LOCK:
        ACTIVE += 1
    upstream = None
    try:
        if not ensure_ready():
            client.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return

        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                upstream = socket.create_connection(("127.0.0.1", backend), timeout=5)
                break
            except OSError:
                time.sleep(0.2)
        if upstream is None:
            client.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return

        payload = rewrite_host(first) if rewrite else first
        if screen_session:
            payload = add_screen_basic_auth(payload)
        upstream.sendall(payload)
        client.settimeout(None)
        upstream.settimeout(None)
        thread = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
        thread.start()
        pipe(upstream, client)
    finally:
        if upstream is not None:
            upstream.close()
        try:
            client.close()
        except OSError:
            pass
        with LOCK:
            ACTIVE -= 1
            LAST_ACTIVE = time.time()


def serve(port: int, backend: int, rewrite: bool, offer_status: bool = False) -> None:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(64)
    log(f"listening :{port} -> 127.0.0.1:{backend}"
        + (" (host rewrite)" if rewrite else ""))
    while True:
        conn, _ = server.accept()
        threading.Thread(target=handle, args=(conn, backend, rewrite, offer_status),
                         daemon=True).start()


def shutdown(_signum=None, _frame=None) -> None:
    STOPPING.set()
    with LOCK:
        stop_stack()
    os._exit(0)


def main() -> int:
    if not CONTROL_TOKEN:
        log("BROWSER_CONTROL_TOKEN is required")
        return 2
    ensure_dirs()
    if not os.path.exists(AUTH_FILE):
        screen_auth()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=reaper, daemon=True).start()
    for port, backend, rewrite in PUBLIC:
        threading.Thread(target=serve, args=(port, backend, rewrite, port == 7070),
                         daemon=True).start()
    log(f"hub ready; stack starts on demand, idles out after {IDLE_SECONDS}s")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
