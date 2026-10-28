#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/lib.sh"

# ============================================================
# Restore a user data volume from a backup
# ============================================================
# Restore overwrites same-named files in the volume and refuses a non-empty
# volume unless --force is given. The user's container must be stopped first.
# ============================================================

USER_NAME=""
INPUT_DIR=""
IMAGE_NAME="ubuntu:24.04"
FORCE=0

usage() {
    cat <<'USAGE'
Usage: scripts/restore.sh --user NAME --input DIR [options]

Options:
  --user NAME     User name (volume ai-dev-NAME-data). Required.
  --input DIR     Absolute backup directory. Required.
  --image IMAGE   Helper image for the tar container (default: ubuntu:24.04)
  --force         Allow overwriting a non-empty volume.
  -h, --help      Show this help.
USAGE
}

log() { printf '%s\n' "$*" >&2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --user|--input|--image) [ "$#" -ge 2 ] || die "$1 requires a value" ;;
    esac
    case "$1" in
        --user)    USER_NAME="$2"; shift 2 ;;
        --input)   INPUT_DIR="$2"; shift 2 ;;
        --image)   IMAGE_NAME="$2"; shift 2 ;;
        --force)   FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *)         die "unknown option: $1 (try --help)" ;;
    esac
done

[ -n "$USER_NAME" ] || die "--user is required"
[ -n "$INPUT_DIR" ] || die "--input is required"

valid_user_name "$USER_NAME" || die "invalid user name: $USER_NAME"
case "$INPUT_DIR" in
    /*|[A-Za-z]:[\\/]*) ;;
    *) die "--input must be an absolute path: $INPUT_DIR" ;;
esac

VOLUME="ai-dev-${USER_NAME}-data"
ARCHIVE="${INPUT_DIR}/${VOLUME}.tar.gz"

[ -f "$ARCHIVE" ] || die "backup archive not found: $ARCHIVE"
INPUT_DIR=$(cd "$INPUT_DIR" && pwd)
HOST_INPUT=$(host_path "$INPUT_DIR")

trap finish_locked_operation EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
acquire_user_lock "$USER_NAME" "$IMAGE_NAME"

# A running container would overwrite the restored data as it writes, so confirm
# it is stopped first.
RUNNING=$(docker ps --filter "volume=${VOLUME}" --format '{{.Names}}') \
    || die "cannot check whether volume is in use: $VOLUME"
if [ -n "$RUNNING" ]; then
    die "container(s) still running for ${USER_NAME}:
${RUNNING}
Stop them before restoring."
fi

# Reject a corrupt archive before creating or changing the destination volume.
docker run --rm --network none --user 0 --entrypoint sh \
    -v "${HOST_INPUT}:/backup:ro" "$IMAGE_NAME" \
    -c 'tar -tzf "/backup/$1" >/dev/null' sh "${VOLUME}.tar.gz" \
    || die "backup archive is unreadable; destination unchanged"

if ! docker volume inspect "$VOLUME" >/dev/null 2>&1; then
    docker volume create \
        --label "ai-dev-env.managed=true" \
        --label "ai-dev-env.user=${USER_NAME}" \
        "$VOLUME" >/dev/null
    log "created volume: $VOLUME"
fi

volume_is_empty() {
    docker run --rm --network none --user 0 --entrypoint sh \
        -v "${VOLUME}:/dst" \
        "$IMAGE_NAME" \
        -c 'entries=$(ls -A /dst) || exit 1; test -z "$entries"'
}

if [ "$FORCE" -ne 1 ] && ! volume_is_empty; then
    die "volume is not empty: $VOLUME (use --force to overwrite)"
fi

log "restoring $VOLUME <- $ARCHIVE"
docker run --rm --network none --user 0 --entrypoint sh \
    -v "${VOLUME}:/dst" \
    -v "${HOST_INPUT}:/backup:ro" \
    "$IMAGE_NAME" \
    -c 'tar -C /dst -xzf "/backup/$1"' sh "${VOLUME}.tar.gz"

log ""
log "done: $VOLUME"
log "   The archive keeps the original uid/gid; run chown inside the container if ownership looks wrong."
