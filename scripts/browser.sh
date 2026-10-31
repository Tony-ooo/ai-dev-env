#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/lib.sh"

# ============================================================
# Manage a user's browser computer container (paired with the dev container, not created by default).
#   scripts/browser.sh start  --user alice --base 30
#   scripts/browser.sh stop   --user alice --base 30
#   scripts/browser.sh status --user alice --base 30
#   scripts/browser.sh screen --user alice --base 30
# ============================================================

BROWSER_REGISTRY=${BROWSER_REGISTRY:-tonyooo/ai-dev-browser}
VERSION=${VERSION:-latest}

COMMAND=${1:-}
[ -n "$COMMAND" ] || { echo "usage: $0 {start|stop|restart|status|screen|logs} --user NAME --base PORT" >&2; exit 2; }
shift

USER_NAME=""
PORT_BASE=""
IMAGE_NAME=""
ARCH=""
MEMORY=${BROWSER_MEMORY:-2g}
CPUS=${BROWSER_CPUS:-2}
IDLE_SECONDS=${BROWSER_IDLE_SECONDS:-600}
# Allow Chromium and the desktop stack to finish their bounded shutdown stages.
STOP_TIMEOUT=25

usage() {
    cat <<'USAGE'
Usage: scripts/browser.sh <command> --user NAME --base PORT [options]

Commands:
  start     create/reuse the network, attach the dev container, start the browser
  stop      stop the browser container (keeps network + volume data)
  restart   recreate the browser container (profile is preserved)
  status    show container and hub state
  screen    print the SSH tunnel command and credentials
  logs      tail the hub and desktop component logs

Options:
  --image IMAGE   Browser image (default: tonyooo/ai-dev-browser:latest)
  -h, --help      Show this help

Env: BROWSER_MEMORY (2g), BROWSER_CPUS (2), BROWSER_IDLE_SECONDS (600), VERSION (latest)

noVNC is never published to the host: reach it through a local forward over the dev container's SSH port.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --user|--base|--image) [ "$#" -ge 2 ] || die "$1 requires a value" ;;
    esac
    case "$1" in
        --user)  USER_NAME="$2"; shift 2 ;;
        --base)  PORT_BASE="$2"; shift 2 ;;
        --image) IMAGE_NAME="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

case "$COMMAND" in
    start|stop|restart|status|screen|logs) ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown command: $COMMAND (start|stop|restart|status|screen|logs)" ;;
esac

[ -n "$USER_NAME" ] || die "--user is required"
[ -n "$PORT_BASE" ] || die "--base is required"
PORT_BASE=$(port_base "$PORT_BASE")
valid_user_name "$USER_NAME" || die "invalid user name: $USER_NAME (letters, digits, dot, underscore, hyphen)"

DEV="ai-dev-${USER_NAME}-${PORT_BASE}22"
BROWSER="ai-dev-${USER_NAME}-${PORT_BASE}-browser"
NET="ai-dev-${USER_NAME}-${PORT_BASE}-net"
VOLUME="ai-dev-${USER_NAME}-data"
SSH_PORT="${PORT_BASE}22"

if [ -z "$IMAGE_NAME" ]; then
    ARCH=$(host_arch) || die "unsupported host arch: $(uname -m)"
    IMAGE_NAME="${BROWSER_REGISTRY}:${VERSION}"
fi

docker inspect "$DEV" >/dev/null 2>&1 || die "dev container not found: $DEV (deploy it first)"

preflight_start() {
    require_browser_docker
    # A browser uses uid/gid 1000. Reject unsupported remapping before changing
    # volume ownership, so an existing user's data remains untouched.
    local dev_uid dev_gid networks dev_ref dev_image browser_image expected_release
    dev_uid=$(docker exec "$DEV" id -u dev) || die "dev container must be running: $DEV"
    dev_gid=$(docker exec "$DEV" id -g dev) || die "cannot read dev group"
    [ "$dev_uid:$dev_gid" = 1000:1000 ] \
        || die "browser requires dev UID/GID 1000:1000 (found $dev_uid:$dev_gid)"
    networks=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$DEV") \
        || die "cannot inspect dev networks"
    if printf '%s\n' "$networks" | grep -Fxq bridge; then
        die "dev still uses the shared bridge; recreate it with scripts/deploy.sh before starting the browser"
    fi
    if [ "$COMMAND" = restart ] || ! docker inspect "$BROWSER" >/dev/null 2>&1; then
        docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 \
            || die "browser image not found: $IMAGE_NAME (build it with scripts/build.sh --with-browser)"
        browser_image=$IMAGE_NAME
    else
        browser_image=$(docker inspect -f '{{.Image}}' "$BROWSER") || die "cannot inspect browser image"
    fi
    if [ "$IMAGE_NAME" = "${BROWSER_REGISTRY}:${VERSION}" ]; then
        dev_ref=$(docker inspect -f '{{.Config.Image}}' "$DEV") || die "cannot inspect dev image"
        case "$dev_ref" in
            *:"${VERSION}"|*:"${VERSION}-${ARCH}")
                dev_image=$(docker inspect -f '{{.Image}}' "$DEV") || die "cannot inspect dev image ID"
                expected_release=""
                [ "$VERSION" = latest ] || expected_release=$VERSION
                check_image_release_pair "$dev_image" "$browser_image" "$expected_release"
                ;;
        esac
    fi
    case "$IDLE_SECONDS" in ''|*[!0-9]*) die "BROWSER_IDLE_SECONDS must be a non-negative integer" ;; esac
}

PREVIOUS=""
NEW_ID=""
ORIGINAL_ID=""
ORIGINAL_TOUCHED=0
WAS_RUNNING=false
rollback() {
    local code=$?
    trap - EXIT INT TERM
    if [ "$code" -ne 0 ] && [ -n "$NEW_ID" ]; then
        docker stop --timeout "$STOP_TIMEOUT" "$NEW_ID" >/dev/null 2>&1 || true
        docker rm -f "$NEW_ID" >/dev/null 2>&1 \
            || printf 'error: failed to remove new browser: %s\n' "$NEW_ID" >&2
    fi
    if [ "$code" -ne 0 ] && [ -n "$ORIGINAL_ID" ]; then
        if [ -n "$PREVIOUS" ]; then
            if ! docker rename "$PREVIOUS" "$BROWSER"; then
                printf 'original browser retained as: %s\n' "$PREVIOUS" >&2
            fi
        fi
        if [ "$ORIGINAL_TOUCHED" -eq 1 ]; then
            if [ "$WAS_RUNNING" = true ]; then
                docker start "$ORIGINAL_ID" >/dev/null \
                    || printf 'error: original browser could not be restarted: %s\n' "$ORIGINAL_ID" >&2
            else
                docker stop --timeout "$STOP_TIMEOUT" "$ORIGINAL_ID" >/dev/null \
                    || printf 'error: original browser could not be stopped: %s\n' "$ORIGINAL_ID" >&2
            fi
        fi
    fi
    release_user_lock || code=1
    exit "$code"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

lock_browser_operation() {
    local image
    image=$(docker inspect -f '{{.Image}}' "$DEV") || die "cannot inspect dev image"
    acquire_user_lock "$USER_NAME" "$image"
}

# ---------------------------------------------------------------- volume
# A fresh volume mountpoint is root-owned and browser runs as uid 1000, so create the directories and hand them over first.
ensure_volume() {
    docker run --rm --network none -u 0 --mount "type=volume,src=${VOLUME},dst=/data" busybox:1 sh -c '
        set -e
        mkdir -p /data/browser/profile /data/browser/downloads /data/browser/state
        chown 1000:1000 /data/browser /data/browser/profile /data/browser/downloads /data/browser/state
        [ -s /data/browser/state/token ] || head -c 32 /dev/urandom | base64 | tr -d "=+/" > /data/browser/state/token
        [ -s /data/browser/state/screen-auth ] || printf "dev:%s" "$(head -c 18 /dev/urandom | base64 | tr -d "=+/\n")" > /data/browser/state/screen-auth
        chmod 600 /data/browser/state/token /data/browser/state/screen-auth
        chown 1000:1000 /data/browser/state/token /data/browser/state/screen-auth'
}

read_state() {
    docker run --rm --network none --mount "type=volume,src=${VOLUME},dst=/data,readonly" busybox:1 cat "/data/browser/state/$1"
}

write_env_file() {
    docker run --rm --network none -i --mount "type=volume,src=${VOLUME},dst=/data" busybox:1 sh -c '
        set -e
        umask 077
        cat > /data/browser/state/env
        chown 1000:1000 /data/browser/state/env
        chmod 600 /data/browser/state/env' <<EOF
BROWSER_HOST=${BROWSER}
BROWSER_CONTROL_PORT=7070
BROWSER_CDP_PORT=9223
BROWSER_SSH_PORT=${SSH_PORT}
EOF
}

# ---------------------------------------------------------------- commands
start_browser() {
    ensure_volume
    write_env_file
    TOKEN=$(read_state token)
    SCREEN_AUTH=$(read_state screen-auth)

    ensure_network "$NET" "$USER_NAME"
    connect_network "$NET" "$DEV"

    if docker inspect "$BROWSER" >/dev/null 2>&1; then
        if [ "$(docker inspect -f '{{.State.Running}}' "$BROWSER")" = "true" ]; then
            echo "browser already running: $BROWSER"
        else
            echo "starting existing browser: $BROWSER"
            docker start "$BROWSER" >/dev/null
        fi
    else
        echo "==> creating $BROWSER"
        # Publish no ports: noVNC is reachable only through the dev container SSH tunnel.
        NEW_ID=$(docker create --name "$BROWSER" \
            --network "$NET" \
            --restart unless-stopped \
            --stop-timeout "$STOP_TIMEOUT" \
            --memory "$MEMORY" --cpus "$CPUS" --pids-limit 2048 \
            --shm-size 256m \
            --cap-drop ALL --security-opt no-new-privileges:true \
            --mount "type=volume,src=${VOLUME},dst=/home/dev/.ai-data,volume-subpath=browser" \
            --mount "type=volume,src=${VOLUME},dst=/home/dev/workspace,volume-subpath=workspace,readonly" \
            -e "BROWSER_CONTROL_TOKEN=${TOKEN}" \
            -e "BROWSER_IDLE_SECONDS=${IDLE_SECONDS}" \
            --label "ai-dev-env.managed=true" \
            --label "ai-dev-env.user=${USER_NAME}" \
            --label "ai-dev-env.role=browser" \
            "$IMAGE_NAME")
        docker start "$NEW_ID" >/dev/null
    fi

    # Hub status does not wake the desktop stack.
    wait_ready "$BROWSER" 40 curl -fsS http://127.0.0.1:7070/__hub/status || return 1

    USER_PART=${SCREEN_AUTH%%:*}
    PASS_PART=${SCREEN_AUTH#*:}
    cat <<EOF

────────────────────────────────────────────
Browser computer is ready

  Container: ${BROWSER}
  Network:   ${NET}
  Idle:      ${IDLE_SECONDS}s without active connections expires old tabs and stops the desktop stack

  Screen (run on the host; no ports are published):
    ssh -T -o ExitOnForwardFailure=yes -p ${SSH_PORT} -L 127.0.0.1:6080:${BROWSER}:6080 dev@<host> "echo SSH connected. Open the live view to check the tunnel.; exec sleep infinity"
    then open http://127.0.0.1:6080/vnc.html
    account: ${USER_PART} / ${PASS_PART}

  Inside the dev container:
    ai-browser status
    ai-browser open https://example.com
    ai-browser snapshot

  Stop:  scripts/browser.sh stop --user ${USER_NAME} --base ${PORT_BASE}
────────────────────────────────────────────
EOF
}

case "$COMMAND" in
    start)
        lock_browser_operation
        preflight_start
        start_browser
        NEW_ID=""
        ;;
    restart)
        lock_browser_operation
        preflight_start
        if docker inspect "$BROWSER" >/dev/null 2>&1; then
            ORIGINAL_ID=$(docker inspect -f '{{.Id}}' "$BROWSER")
            WAS_RUNNING=$(docker inspect -f '{{.State.Running}}' "$BROWSER")
            previous="${BROWSER}-previous-$$"
            ORIGINAL_TOUCHED=1
            docker stop --timeout "$STOP_TIMEOUT" "$BROWSER" >/dev/null
            docker rename "$BROWSER" "$previous"
            PREVIOUS=$previous
        fi
        start_browser
        if [ -n "$PREVIOUS" ]; then
            docker rm "$PREVIOUS" >/dev/null
            PREVIOUS=""
            ORIGINAL_ID=""
        fi
        NEW_ID=""
        ;;
    stop)
        lock_browser_operation
        if docker inspect "$BROWSER" >/dev/null 2>&1; then
            docker stop --timeout "$STOP_TIMEOUT" "$BROWSER" >/dev/null
            echo "stopped $BROWSER (data kept in volume $VOLUME)"
        else
            echo "browser container not found: $BROWSER"
        fi
        ;;
    status)
        if ! docker inspect "$BROWSER" >/dev/null 2>&1; then
            echo "browser: not created ($BROWSER)"
            exit 0
        fi
        echo "browser: $(docker inspect -f '{{.State.Status}}' "$BROWSER")"
        echo "image:   $(docker inspect -f '{{.Config.Image}}' "$BROWSER")"
        echo "tunnel:  ssh -T -o ExitOnForwardFailure=yes -p ${SSH_PORT} -L 127.0.0.1:6080:${BROWSER}:6080 dev@<host> \"echo SSH connected. Open the live view to check the tunnel.; exec sleep infinity\""
        # Hub status does not wake the desktop stack.
        if docker exec "$BROWSER" curl -fsS http://127.0.0.1:7070/__hub/status 2>/dev/null; then
            echo
        else
            echo "hub:     unreachable"
        fi
        ;;
    logs)
        docker inspect "$BROWSER" >/dev/null 2>&1 || die "browser container not found: $BROWSER"
        echo "=== container log ==="
        docker logs --tail 80 "$BROWSER" 2>&1 || true
        echo
        echo "=== desktop components ==="
        docker exec "$BROWSER" sh -c '
            for f in /tmp/browser/*.log; do
                [ -f "$f" ] || continue
                echo "--- $f ---"
                tail -n 20 "$f"
            done' 2>/dev/null || true
        ;;
    screen)
        SCREEN_AUTH=$(read_state screen-auth 2>/dev/null || true)
        [ -n "$SCREEN_AUTH" ] || die "no screen credentials yet; run start first"
        cat <<EOF
Create the tunnel on the host:
  ssh -T -o ExitOnForwardFailure=yes -p ${SSH_PORT} -L 127.0.0.1:6080:${BROWSER}:6080 dev@<host> "echo SSH connected. Open the live view to check the tunnel.; exec sleep infinity"

Then open it in a local browser (auto-connect and scaling):
  http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale&reconnect=true

username: ${SCREEN_AUTH%%:*}
password: ${SCREEN_AUTH#*:}
EOF
        ;;
esac
