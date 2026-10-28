#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/lib.sh"

# ============================================================
# Back up a single user's data volume
# ============================================================

USER_NAME=""
OUTPUT_DIR=""
IMAGE_NAME="ubuntu:24.04"

usage() {
    cat <<'USAGE'
Usage: scripts/backup.sh --user NAME --output DIR [options]

Options:
  --user NAME     User name (volume ai-dev-NAME-data). Required.
  --output DIR    Absolute output directory. Required.
  --image IMAGE   Helper image for the tar container (default: ubuntu:24.04)
  -h, --help      Show this help.
USAGE
}

log() { printf '%s\n' "$*" >&2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --user|--output|--image) [ "$#" -ge 2 ] || die "$1 requires a value" ;;
    esac
    case "$1" in
        --user)    USER_NAME="$2"; shift 2 ;;
        --output)  OUTPUT_DIR="$2"; shift 2 ;;
        --image)   IMAGE_NAME="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *)         die "unknown option: $1 (try --help)" ;;
    esac
done

[ -n "$USER_NAME" ]  || die "--user is required"
[ -n "$OUTPUT_DIR" ] || die "--output is required"

valid_user_name "$USER_NAME" || die "invalid user name: $USER_NAME"
case "$OUTPUT_DIR" in
    /*|[A-Za-z]:[\\/]*) ;;
    *) die "--output must be an absolute path: $OUTPUT_DIR" ;;
esac

VOLUME="ai-dev-${USER_NAME}-data"
ARCHIVE="${VOLUME}.tar.gz"

docker volume inspect "$VOLUME" >/dev/null 2>&1 || die "volume not found: $VOLUME"

TMP_ARCHIVE=""
finish_backup() {
    local code=$?
    trap - EXIT INT TERM
    if [ -n "$TMP_ARCHIVE" ]; then
        rm -f -- "$TMP_ARCHIVE" || code=1
    fi
    release_user_lock || code=1
    exit "$code"
}
trap finish_backup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
acquire_user_lock "$USER_NAME" "$IMAGE_NAME"

# A running Chromium or editor keeps writing to the volume, so tar can capture an
# inconsistent state.
RUNNING=$(docker ps --filter "volume=${VOLUME}" --format '{{.Names}}') \
    || die "cannot check whether volume is in use: $VOLUME"
if [ -n "$RUNNING" ]; then
    die "container(s) still running for ${USER_NAME}:
${RUNNING}
Stop them before backing up, so the archive is consistent."
fi
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR=$(cd "$OUTPUT_DIR" && pwd)

log "backing up $VOLUME -> ${OUTPUT_DIR}/${ARCHIVE}"
# Create the file as the invoking host user, rather than as container root.
# Keep it private and publish it atomically only after the stream is validated.
umask 077
TMP_ARCHIVE=$(mktemp "${OUTPUT_DIR}/.${ARCHIVE}.tmp.XXXXXX")
docker run --rm --network none --user 0 --entrypoint sh \
    -v "${VOLUME}:/data:ro" "$IMAGE_NAME" \
    -eu -c 'tar -C /data -czf - .' >"$TMP_ARCHIVE"
docker run --rm -i --network none --user 0 --entrypoint sh \
    "$IMAGE_NAME" -eu -c 'tar -tzf - >/dev/null' <"$TMP_ARCHIVE"
mv -f -- "$TMP_ARCHIVE" "${OUTPUT_DIR}/${ARCHIVE}"
TMP_ARCHIVE=""

log ""
log "done: ${OUTPUT_DIR}/${ARCHIVE}"
