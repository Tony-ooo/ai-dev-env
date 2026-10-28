#!/usr/bin/env bash
# ai-dev-env browser computer: PID 1 entry.
# Only a small hub stays resident; the desktop stack starts on the first
# connection and stops after BROWSER_IDLE_SECONDS (default 600).
set -euo pipefail

export DISPLAY="${DISPLAY:-:1}"
export HOME="${HOME:-/home/dev}"

STATE_DIR="$HOME/.ai-data/state"
mkdir -p /tmp/browser /tmp/.X11-unix /tmp/fluxbox-home \
         "$STATE_DIR" \
         "$HOME/.ai-data/profile" "$HOME/.ai-data/downloads"

# Chromium downloads to $HOME/Downloads by default; symlink it into the volume so
# ~/downloads in the dev container shows the same files.
if [ -d "$HOME/Downloads" ] && [ ! -L "$HOME/Downloads" ]; then
    if ! cp -a "$HOME/Downloads"/. "$HOME/.ai-data/downloads"/; then
        printf '[browser] download migration failed; source retained\n' >&2
        exit 1
    fi
    rm -rf "$HOME/Downloads"
fi
ln -sfn "$HOME/.ai-data/downloads" "$HOME/Downloads"

if [ -z "${BROWSER_CONTROL_TOKEN:-}" ]; then
    umask 077
    BROWSER_CONTROL_TOKEN="$(head -c 24 /dev/urandom | base64 | tr -d '=+/\n')"
    printf '%s' "$BROWSER_CONTROL_TOKEN" > "$STATE_DIR/token"
    export BROWSER_CONTROL_TOKEN
    printf '[browser] BROWSER_CONTROL_TOKEN missing; generated %s\n' "$STATE_DIR/token" >&2
fi

# websockify compares the "user:pass" string itself; make sure one exists.
if [ ! -s "$STATE_DIR/screen-auth" ]; then
    umask 077
    printf 'dev:%s' "$(head -c 18 /dev/urandom | base64 | tr -d '=+/\n')" > "$STATE_DIR/screen-auth"
    printf '[browser] screen-auth missing; generated %s\n' "$STATE_DIR/screen-auth" >&2
fi

exec python3 /usr/local/bin/browser-hub
