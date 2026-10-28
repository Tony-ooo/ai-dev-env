"""Bounded stdout/stderr capture for browser desktop subprocesses."""

import os
import subprocess
import sys
import threading

LOG_LIMIT = 2 * 1024 * 1024


def capture_log(stream, path: str, limit: int = LOG_LIMIT) -> None:
    """Drain a pipe continuously, retaining at most two limit-sized files."""
    handle = None
    with stream:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            for existing in (path, path + ".1"):
                if os.path.exists(existing) and os.path.getsize(existing) > limit:
                    with open(existing, "r+b") as old:
                        old.seek(-limit, os.SEEK_END)
                        tail = old.read(limit)
                        old.seek(0)
                        old.write(tail)
                        old.truncate()
            handle = open(path, "ab", buffering=0)
            size = handle.tell()
            while data := stream.read1(65536):
                while data:
                    if size >= limit:
                        handle.close()
                        os.replace(path, path + ".1")
                        handle = open(path, "wb", buffering=0)
                        size = 0
                    chunk, data = data[:limit - size], data[limit - size:]
                    handle.write(chunk)
                    size += len(chunk)
        except OSError as error:
            print(f"[browser-log] cannot write {path}: {error}; discarding output", file=sys.stderr)
            # A full disk must not stop pipe consumption or block the application.
            try:
                while stream.read1(65536):
                    pass
            except OSError:
                pass
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass


def spawn_logged(argv: list[str], path: str, env: dict | None = None) -> subprocess.Popen:
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)
    threading.Thread(target=capture_log, args=(proc.stdout, path), daemon=True).start()
    return proc
