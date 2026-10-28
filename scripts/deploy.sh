#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/lib.sh"

# ============================================================
# Deploy one isolated dev container per user
# ============================================================
# One user = one container + one data volume (ai-dev-<user>-data).
# ============================================================

REGISTRY=${REGISTRY:-tonyooo/ai-dev-env}
VERSION=${VERSION:-latest}

USER_NAME=""
PORT_BASE=""
USER_PASSWORD=""
IMAGE_NAME=""
CPUS=""
MEMORY=""
PIDS_LIMIT="4096"
BIND_ADDRESS=${BIND_ADDRESS:-127.0.0.1}
WITH_BROWSER=0
RECREATE=0
GPU=auto

usage() {
    cat <<'USAGE'
Usage: scripts/deploy.sh --user NAME --port BASE [options]

Required:
  --user NAME         User name (container and volume name)
  --port BASE         Port base; SSH is exposed on <BASE>22

Options:
  --image IMAGE       Image to run (default: $REGISTRY:$VERSION)
  --password PW       SSH password for dev (default: random)
  --cpu N             CPU limit
  --memory SIZE       Memory limit, e.g. 4g
  --pids-limit N      Max PIDs (default: 4096)
  --bind ADDR         Host bind address (default: 127.0.0.1)
  --with-browser      Also create the browser computer container
  --recreate          Replace an existing container (keeps its SSH password)
  --no-gpu            Disable GPU passthrough even if available
  -h, --help          Show this help

Access:
  ssh dev@<host> -p <BASE>22
  ssh -T -o ExitOnForwardFailure=yes -p <BASE>22 -L 127.0.0.1:6080:<browser>:6080 dev@<host> "echo SSH connected. Open the live view to check the tunnel.; exec sleep infinity"

Data:
  Volume ai-dev-<user>-data -> /home/dev/.ai-data
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --user|--port|--password|--image|--cpu|--memory|--pids-limit|--bind)
            [ "$#" -ge 2 ] || die "$1 requires a value" ;;
    esac
    case "$1" in
        --user)        USER_NAME="$2"; shift 2 ;;
        --port)        PORT_BASE="$2"; shift 2 ;;
        --password)    USER_PASSWORD="$2"; shift 2 ;;
        --image)       IMAGE_NAME="$2"; shift 2 ;;
        --cpu)         CPUS="$2"; shift 2 ;;
        --memory)      MEMORY="$2"; shift 2 ;;
        --pids-limit)  PIDS_LIMIT="$2"; shift 2 ;;
        --bind)        BIND_ADDRESS="$2"; shift 2 ;;
        --with-browser) WITH_BROWSER=1; shift ;;
        --recreate)    RECREATE=1; shift ;;
        --no-gpu)      GPU=off; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             die "unknown option: $1 (try --help)" ;;
    esac
done

[ -n "$USER_NAME" ] || die "--user is required"
[ -n "$PORT_BASE" ] || die "--port is required"

PORT_BASE=$(port_base "$PORT_BASE")
if [[ ! "$BIND_ADDRESS" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    die "--bind must be an IPv4 address: $BIND_ADDRESS"
fi
IFS=. read -r -a OCTETS <<< "$BIND_ADDRESS"
for octet in "${OCTETS[@]}"; do
    [ "$((10#$octet))" -le 255 ] || die "invalid IPv4 address: $BIND_ADDRESS"
done

DATA_VOLUME="ai-dev-${USER_NAME}-data"
CONTAINER="ai-dev-${USER_NAME}-${PORT_BASE}22"
BROWSER_HOST="ai-dev-${USER_NAME}-${PORT_BASE}-browser"
NETWORK="ai-dev-${USER_NAME}-${PORT_BASE}-net"

valid_user_name "$USER_NAME" || die "invalid user name: $USER_NAME (letters, digits, dot, underscore, hyphen)"

if [ -z "$IMAGE_NAME" ]; then
    host_arch >/dev/null || die "unsupported host arch: $(uname -m)"
    IMAGE_NAME="${REGISTRY}:${VERSION}"
fi

docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 \
    || die "image not found: $IMAGE_NAME (build it first or pass --image)"
if [ "$WITH_BROWSER" -eq 1 ]; then
    require_browser_docker
    host_arch >/dev/null || die "unsupported host arch: $(uname -m)"
    BROWSER_IMAGE="${BROWSER_REGISTRY:-tonyooo/ai-dev-browser}:${VERSION}"
    docker image inspect "$BROWSER_IMAGE" >/dev/null 2>&1 \
        || die "browser image not found: $BROWSER_IMAGE"
    if [ "$IMAGE_NAME" = "${REGISTRY}:${VERSION}" ]; then
        EXPECTED_RELEASE=""
        [ "$VERSION" = latest ] || EXPECTED_RELEASE=$VERSION
        check_image_release_pair "$IMAGE_NAME" "$BROWSER_IMAGE" "$EXPECTED_RELEASE"
        if docker inspect "$BROWSER_HOST" >/dev/null 2>&1; then
            EXISTING_BROWSER_IMAGE=$(docker inspect -f '{{.Image}}' "$BROWSER_HOST") \
                || die "cannot inspect existing browser image: $BROWSER_HOST"
            check_image_release_pair "$IMAGE_NAME" "$EXISTING_BROWSER_IMAGE" "$EXPECTED_RELEASE"
        fi
    fi
fi

HAD_CONTAINER=0
ORIGINAL_ID=""
ORIGINAL_TOUCHED=0
WAS_RUNNING=false
PREVIOUS=""
NEW_ID=""
RECOVERY_ACTIVE=0
rollback() {
    local code=$?
    trap - EXIT INT TERM
    if [ "$RECOVERY_ACTIVE" -eq 1 ]; then
        offline_home_archive abort transaction </dev/null \
            || printf 'error: recovery rollback failed; retry deployment before using the volume\n' >&2
    fi
    if [ "$code" -ne 0 ] && [ -n "$NEW_ID" ]; then
        docker rm -f "$NEW_ID" >/dev/null 2>&1 \
            || printf 'error: failed to remove new container: %s\n' "$NEW_ID" >&2
    fi
    if [ "$code" -ne 0 ] && [ -n "$ORIGINAL_ID" ]; then
        if [ -n "$PREVIOUS" ]; then
            if ! docker rename "$PREVIOUS" "$CONTAINER"; then
                printf 'original container retained as: %s\n' "$PREVIOUS" >&2
            fi
        fi
        if [ "$ORIGINAL_TOUCHED" -eq 1 ]; then
            if [ "$WAS_RUNNING" = true ]; then
                docker start "$ORIGINAL_ID" >/dev/null \
                    || printf 'error: original container could not be restarted: %s\n' "$ORIGINAL_ID" >&2
            else
                docker stop "$ORIGINAL_ID" >/dev/null \
                    || printf 'error: original container could not be stopped: %s\n' "$ORIGINAL_ID" >&2
            fi
        fi
    fi
    release_user_lock || code=1
    exit "$code"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
acquire_user_lock "$USER_NAME" "$IMAGE_NAME"

# Check all instances for this user, including stopped ones: another port must
# not start a second writer on the same data volume or Chromium profile.
EXISTING=$(docker ps -a --filter "label=ai-dev-env.user=${USER_NAME}" --format '{{.Names}}') \
    || die "cannot list existing containers"
while IFS= read -r existing; do
    [ -n "$existing" ] || continue
    case "$existing" in
        "$CONTAINER"|"$BROWSER_HOST"|"$USER_LOCK_NAME") ;;
        *) die "user already has another instance: $existing (remove or migrate it before changing ports)" ;;
    esac
done <<< "$EXISTING"

if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    HAD_CONTAINER=1
    [ "$RECREATE" -eq 1 ] || die "container already exists: $CONTAINER (use --recreate)"
    ORIGINAL_ID=$(docker inspect -f '{{.Id}}' "$CONTAINER")
    WAS_RUNNING=$(docker inspect -f '{{.State.Running}}' "$CONTAINER")
fi

# Reuse the existing SSH password on --recreate so a rebuild does not silently
# lock the user out.
if [ "$HAD_CONTAINER" -eq 1 ]; then
    if [ -z "$USER_PASSWORD" ]; then
        USER_PASSWORD=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \
            | sed -n 's/^USER_PASSWORD=//p' | head -1)
        if [ -n "$USER_PASSWORD" ]; then
            printf 'reusing existing SSH password\n' >&2
        fi
    fi
fi

GENERATED=0
if [ -z "$USER_PASSWORD" ]; then
    USER_PASSWORD=$(openssl rand -base64 32 | tr -d '=+/.' | cut -c1-32)
    GENERATED=1
fi

offline_home_archive() {
    local mode=$1 name=$2 stream_fd helper_pid copy_error copy_code helper_code
    local helper_name="${CONTAINER}-persist-${2//[^A-Za-z0-9_-]/-}"
    if [ "$mode" = begin ] || [ "$mode" = commit ] || [ "$mode" = abort ]; then
        docker run --rm -i --name "$helper_name" --network none \
            --read-only --user 0 --cap-drop ALL --cap-add CHOWN --cap-add FOWNER \
            --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true \
            --memory 256m --cpus 1 --pids-limit 32 --tmpfs /tmp:rw,nosuid,nodev,size=32m \
            --mount "type=volume,src=${DATA_VOLUME},dst=/data" \
            --entrypoint /usr/bin/timeout "$IMAGE_NAME" 300 /usr/bin/env -i \
            PATH=/usr/bin:/bin HOME=/tmp TMPDIR=/tmp \
            python3 -c "$ARCHIVE_HELPER" "$mode" "$name" </dev/null
        return $?
    fi
    # Only the tar stream crosses this pipe. No archive or configuration value
    # is written to the host or printed. The helper sees one volume only.
    exec {stream_fd}> >(docker run --rm -i --name "$helper_name" --network none \
        --read-only --user 0 --cap-drop ALL --cap-add CHOWN --cap-add FOWNER \
        --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true \
        --memory 256m --cpus 1 --pids-limit 32 --tmpfs /tmp:rw,nosuid,nodev,size=32m \
        --mount "type=volume,src=${DATA_VOLUME},dst=/data" \
        --entrypoint /usr/bin/timeout "$IMAGE_NAME" 300 /usr/bin/env -i \
        PATH=/usr/bin:/bin HOME=/tmp TMPDIR=/tmp \
        python3 -c "$ARCHIVE_HELPER" "$mode" "$name")
    helper_pid=$!
    if copy_error=$(docker cp "$CONTAINER:/home/dev/$name" - 2>&1 >&"$stream_fd"); then
        copy_code=0
    else
        copy_code=$?
    fi
    exec {stream_fd}>&-
    if wait "$helper_pid"; then helper_code=0; else helper_code=$?; fi
    if [ "$copy_code" -eq 0 ] && [ "$helper_code" -eq 0 ]; then
        return 0
    fi
    # A missing optional mapping is different from a daemon or tar failure.
    # Accept only Docker's exact file-not-found response plus an empty stream.
    if [ "$mode" = stage ] && [ "$copy_code" -ne 0 ] && [ "$helper_code" -eq 44 ] && \
       [ "$copy_error" = "Error response from daemon: Could not find the file /home/dev/$name in container $CONTAINER" ]; then
        return 0
    fi
    printf 'error: offline recovery failed for %s; original container retained\n' "$name" >&2
    return 1
}

persist_stopped_legacy_home() {
    local name
    ARCHIVE_HELPER=$(cat "$SCRIPT_DIR/persist_home_archive.py") || return 1
    RECOVERY_ACTIVE=1
    offline_home_archive begin transaction || return 1
    offline_home_archive verify workspace || return 1
    for name in workspace downloads .claude .codex .vscode-server .ssh .agents .config \
                .claude.json .bashrc.extra .gitconfig .npmrc; do
        offline_home_archive stage "$name" || return 1
    done
    offline_home_archive commit transaction || return 1
    RECOVERY_ACTIVE=0
}

if [ "$HAD_CONTAINER" -eq 1 ]; then
    PERSISTED_OFFLINE=0
    if [ "$WAS_RUNNING" != true ]; then
        # The marker is non-secret. Do not start an old entrypoint that could
        # discard atomically replaced configuration before it can be recovered.
        if docker cp "$CONTAINER:/var/lib/ai-dev-env/home-config-initialized" - >/dev/null 2>&1; then
            ORIGINAL_TOUCHED=1
            docker start "$CONTAINER" >/dev/null
            wait_ready "$CONTAINER" 30 bash -c ': > /dev/tcp/127.0.0.1/22' \
                || die "original container did not become ready; it has been retained"
        else
            persist_stopped_legacy_home || die "stopped legacy home could not be recovered; original container retained"
            PERSISTED_OFFLINE=1
        fi
    fi
    if [ "$PERSISTED_OFFLINE" -eq 1 ]; then
        printf 'recovered stopped legacy home without starting the old entrypoint\n' >&2
    elif docker exec -u 0 "$CONTAINER" test -f /var/lib/ai-dev-env/home-config-initialized; then
        docker exec -u 0 "$CONTAINER" /usr/local/bin/entrypoint.sh --persist-home \
            || die "home persistence failed; original container retained"
    else
        # Compatibility for an initialized, running legacy image. Copy only
        # replaced mappings; an intact symlink already writes to the volume.
        docker exec -u 0 "$CONTAINER" sh -eu -c '
            data=/home/dev/.ai-data
            [ "$(readlink /home/dev/workspace)" = "$data/workspace" ] || exit 1
            for pair in ".claude.json:home-config/claude.json" ".bashrc.extra:home-config/bashrc.extra" ".gitconfig:home-config/gitconfig" ".npmrc:home-config/npmrc" ".config:home-config/.config" ".claude:.claude" ".codex:.codex" ".vscode-server:.vscode-server" ".agents:agents" ".ssh:ssh" "downloads:browser/downloads"; do
                src=/home/dev/${pair%%:*}; dst=$data/${pair#*:}
                [ ! -L "$src" ] || continue
                if [ -d "$src" ]; then
                    mkdir -p "$dst"
                    cp -a "$src"/. "$dst"/
                elif [ -f "$src" ]; then
                    cp -a "$src" "$dst"
                fi
            done' || die "legacy home persistence failed; original container retained"
    fi
fi

if ! docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1; then
    docker volume create \
        --label "ai-dev-env.managed=true" \
        --label "ai-dev-env.user=${USER_NAME}" \
        "$DATA_VOLUME" >/dev/null
    echo "created volume: $DATA_VOLUME"
fi

ensure_network "$NETWORK" "$USER_NAME"

RUN_ARGS=(
    --name "$CONTAINER"
    --network "$NETWORK"
    --restart unless-stopped
    --add-host host.docker.internal:host-gateway
    --label "ai-dev-env.managed=true"
    --label "ai-dev-env.user=${USER_NAME}"
    --label "ai-dev-env.role=dev"
    --pids-limit "$PIDS_LIMIT"
    --mount "type=volume,src=${DATA_VOLUME},dst=/home/dev/.ai-data"
    -e "USER_PASSWORD=${USER_PASSWORD}" \
    -e "BROWSER_HOST=${BROWSER_HOST}" \
    -e "BROWSER_CONTROL_PORT=7070" \
    -e "BROWSER_CDP_PORT=9223" \
    -e "BROWSER_SSH_PORT=${PORT_BASE}22" \
    -p "${BIND_ADDRESS}:${PORT_BASE}22:22"
)

if [ -n "$CPUS" ]; then
    RUN_ARGS+=(--cpus "$CPUS")
fi
if [ -n "$MEMORY" ]; then
    RUN_ARGS+=(--memory "$MEMORY")
fi

GPU_STATE="disabled"
if [ "$GPU" = auto ] && nvidia-smi -L >/dev/null 2>&1; then
    RUN_ARGS+=(--gpus all)
    GPU_STATE="enabled (all)"
fi

if [ "$HAD_CONTAINER" -eq 1 ]; then
    previous="${CONTAINER}-previous-$$"
    ORIGINAL_TOUCHED=1
    docker stop "$CONTAINER" >/dev/null
    docker rename "$CONTAINER" "$previous"
    PREVIOUS=$previous
fi

echo "==> deploying $CONTAINER"
# MSYS_NO_PATHCONV: Git Bash (MSYS2) would rewrite container paths such as
# dst=/home/dev/.ai-data into Windows paths. No effect on other platforms.
if ! NEW_ID=$(docker create "${RUN_ARGS[@]}" "$IMAGE_NAME"); then
    die "container creation failed
  - port busy?      lsof -i:${PORT_BASE}22
  - name conflict?  docker ps -a | grep ${USER_NAME}
  - image present?  docker images | grep ${IMAGE_NAME}"
fi
docker start "$NEW_ID" >/dev/null || die "container start failed: $CONTAINER"

# Wait for SSH readiness so the user can connect immediately.
wait_ready "$CONTAINER" 30 bash -c ': > /dev/tcp/127.0.0.1/22' || exit 1

if [ -n "$PREVIOUS" ]; then
    docker rm "$PREVIOUS" >/dev/null
    PREVIOUS=""
    ORIGINAL_ID=""
fi
NEW_ID=""

# The dev state is now stable. The optional browser command acquires its own
# lock, so release ours before invoking that separate operation.
release_user_lock

cat <<EOF

────────────────────────────────────────────
Dev environment for ${USER_NAME} is ready

  Login:    ssh dev@${BIND_ADDRESS} -p ${PORT_BASE}22
  Password: ${USER_PASSWORD}$( [ "$GENERATED" -eq 1 ] && echo "  (generated now, save it)" )
  Container: ${CONTAINER}
  Volume:    ${DATA_VOLUME}

  Listen:  ${BIND_ADDRESS}:${PORT_BASE}22
  CPU:     ${CPUS:-unlimited}
  Memory:  ${MEMORY:-unlimited}
  GPU:     ${GPU_STATE}
  PIDs:    ${PIDS_LIMIT}

Notes:
  - Bound to 127.0.0.1 by default. For remote access use --bind 0.0.0.0 plus a firewall allowlist.
  - Root inside the container: docker exec -u root -it ${CONTAINER} bash
  - Data lives in the volume; deleting it is permanent. Back up with scripts/backup.sh.
────────────────────────────────────────────
EOF

if [ "$WITH_BROWSER" -eq 1 ]; then
    echo
    "$SCRIPT_DIR/browser.sh" start --user "$USER_NAME" --base "$PORT_BASE"
fi
