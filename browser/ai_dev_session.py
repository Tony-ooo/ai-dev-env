"""Expire tab restoration without touching persistent website data.

The marker survives hub/container restarts. Only call clear_tab_session after
all Chromium processes using this profile have exited; keep the marker until a
fresh browser is ready so an interrupted cleanup can be retried.
"""

import os
from pathlib import Path
import re


def expiry_marker(state_dir: str) -> Path:
    return Path(state_dir) / "tabs-expired"


def mark_expired(state_dir: str) -> None:
    marker = expiry_marker(state_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("wb") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    # Persist the directory entry as well as the file before stopping Chromium.
    if os.name == "posix":
        fd = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def clear_tab_session(profile_dir: str) -> None:
    """Remove only Chromium tab/session restoration files, never whole profiles."""
    root = Path(profile_dir)
    if root.is_symlink():
        raise RuntimeError("Refusing to clean a symlinked browser profile")
    if not root.exists():
        return
    for profile in root.iterdir():
        if profile.name != "Default" and not re.fullmatch(r"Profile \d+", profile.name):
            continue
        if profile.is_symlink():
            raise RuntimeError("Refusing to clean a symlinked browser subprofile")
        if not profile.is_dir():
            continue
        sessions = profile / "Sessions"
        if sessions.is_symlink():
            raise RuntimeError("Refusing to clean a symlinked session directory")
        candidates = [profile / name for name in
                      ("Current Session", "Current Tabs", "Last Session", "Last Tabs")]
        if sessions.is_dir():
            candidates.extend(path for path in sessions.iterdir()
                              if re.fullmatch(r"(?:Session|Tabs)_\d+", path.name))
        for path in candidates:
            # unlink removes a file symlink itself, never its destination.
            path.unlink(missing_ok=True)
