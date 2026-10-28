#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Build and push images
# ============================================================
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# Docker Desktop on Windows needs a Windows-style path for the build context.
# Git Bash converts /d/foo to D:/foo by default, but an externally set
# MSYS_NO_PATHCONV=1 (which is only correct for docker run container paths)
# switches that off and docker then fails with "path /d/foo not found".
# Converting explicitly keeps the result independent of the caller's setting.
. "$ROOT/scripts/lib.sh"

# Git for Windows also needs this host path while MSYS_NO_PATHCONV is set.
BUILD_CONTEXT=$(host_path "$ROOT")
BUILD_FILE=$(host_path "$ROOT/Dockerfile")

REGISTRY=${REGISTRY:-tonyooo/ai-dev-env}
BROWSER_REGISTRY=${BROWSER_REGISTRY:-tonyooo/ai-dev-browser}
VERSION=${VERSION:-}
RELEASE_SERIES=${RELEASE_SERIES:-0.1}
RELEASE_BASE=${RELEASE_BASE:-@root}
IMAGE_GIT_REMOTE=${IMAGE_GIT_REMOTE:-origin}
PUSH=0
WITH_BROWSER=0
VERSION_LOCK=""
REVISION=""
IS_LATEST=0

release_lock() {
    if [ -n "$VERSION_LOCK" ]; then
        rmdir "$VERSION_LOCK" || return 1
        VERSION_LOCK=""
    fi
}

finish_build() {
    local code=$?
    trap - EXIT INT TERM
    release_lock || code=1
    exit "$code"
}

image_release_state() {
    local image=$1 error_file status labels expected
    local -a manifest_options=()
    expected="${VERSION}|${REVISION}"
    case "$image" in
        localhost:*|127.0.0.1:*) manifest_options+=(--insecure) ;;
    esac
    if docker image inspect "$image" >/dev/null 2>&1; then
        labels=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}|{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image") \
            || die "cannot inspect local image release: $image"
        [ "$labels" = "$expected" ] || die "local image tag belongs to another release: $image"
    fi
    error_file=$(mktemp "${VERSION_STATE_DIR}/.manifest-error.XXXXXX") \
        || die 'cannot create registry check file'
    if docker manifest inspect "${manifest_options[@]}" "$image" >/dev/null 2>"$error_file"; then
        rm -f "$error_file"
        # Pull the existing immutable release so retries use its exact content.
        docker pull "$image" >&2 || die "cannot pull existing image release: $image"
        labels=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}|{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image") \
            || die "cannot inspect existing image release: $image"
        [ "$labels" = "$expected" ] || die "registry image tag belongs to another release: $image"
        printf 'published'
        return 0
    fi
    status=0
    grep -Eqi 'no such manifest:|manifest unknown' "$error_file" || status=2
    if [ "$status" -eq 2 ]; then
        printf 'error: cannot determine whether image tag exists: %s\n' "$image" >&2
        cat "$error_file" >&2
    fi
    rm -f "$error_file"
    [ "$status" -eq 0 ] || return "$status"
    printf 'new'
}

check_release_source() {
    local status remote_info remote_revision history
    status=$(git -C "$BUILD_CONTEXT" status --porcelain --untracked-files=normal) \
        || die 'cannot inspect the Git working tree'
    [ -z "$status" ] || die 'image releases require a clean Git working tree'
    remote_info=$(git -C "$BUILD_CONTEXT" ls-remote --heads "$IMAGE_GIT_REMOTE" refs/heads/main 2>/dev/null) \
        || die 'cannot read the remote main branch'
    remote_revision=${remote_info%%$'\t'*}
    [ -n "$remote_revision" ] || die 'remote main branch was not found'
    IS_LATEST=0
    if [ "$remote_revision" = "$REVISION" ]; then
        IS_LATEST=1
    else
        git -C "$BUILD_CONTEXT" cat-file -e "${remote_revision}^{commit}" 2>/dev/null \
            || die 'fetch remote main before releasing an earlier commit'
        history=$(git -C "$BUILD_CONTEXT" rev-list --first-parent "$remote_revision") \
            || die 'cannot inspect remote main commit history'
        grep -Fxq "$REVISION" <<< "$history" \
            || die 'image releases require a commit on remote main first-parent history'
    fi
}

reserve_version() {
    local base history patch image
    [[ "$RELEASE_SERIES" =~ ^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$ ]] \
        || die 'RELEASE_SERIES must be MAJOR.MINOR, for example 0.1'
    VERSION_STATE_DIR=${AI_DEV_VERSION_STATE_DIR:-${XDG_STATE_HOME:-${HOME:?HOME is required}/.local/state}/ai-dev-env}
    mkdir -p "$VERSION_STATE_DIR"
    VERSION_LOCK="${VERSION_STATE_DIR}/image-release-${ARCH}.lock"
    if ! mkdir "$VERSION_LOCK" 2>/dev/null; then
        VERSION_LOCK=""
        die 'another image release is running, or its lock needs inspection'
    fi
    REVISION=$(git -C "$BUILD_CONTEXT" rev-parse --verify 'HEAD^{commit}') \
        || die 'cannot determine the current Git commit'
    check_release_source
    [ "$(git -C "$BUILD_CONTEXT" rev-parse --is-shallow-repository)" = false ] \
        || die 'image releases require a complete Git history (fetch --unshallow)'
    if [ "$RELEASE_BASE" = @root ]; then
        patch=$(git -C "$BUILD_CONTEXT" rev-list --first-parent --count "$REVISION") \
            || die 'cannot calculate image version'
    else
        base=$(git -C "$BUILD_CONTEXT" rev-parse --verify "${RELEASE_BASE}^{commit}") \
            || die 'release base is not present in this checkout'
        history=$(git -C "$BUILD_CONTEXT" rev-list --first-parent "$REVISION") \
            || die 'cannot inspect main commit history'
        grep -Fxq "$base" <<< "$history" || die 'release base is not on the main first-parent history'
        patch=$(git -C "$BUILD_CONTEXT" rev-list --first-parent --count "$base..$REVISION") \
            || die 'cannot calculate image version'
    fi
    [[ "$patch" =~ ^[1-9][0-9]{0,8}$ ]] || die 'the current commit must follow the release base'
    VERSION="${RELEASE_SERIES}.${patch}"
    image="${REGISTRY}:${VERSION}-${ARCH}"
    DEV_STATE=$(image_release_state "$image") || return 1
    if [ "$WITH_BROWSER" -eq 1 ]; then
        image="${BROWSER_REGISTRY}:${VERSION}-${ARCH}"
        BROWSER_STATE=$(image_release_state "$image") || return 1
    fi
    printf 'image version %s for commit %s\n' "$VERSION" "$REVISION"
}

usage() {
    cat <<'USAGE'
Usage: scripts/build.sh [options]

Builds for the architecture of the machine running it, and pushes only that.

Options:
  --with-browser    Also build the browser-computer image.
  --push            Push the built image(s).
  -h, --help        Show this help.

Env:
  REGISTRY          Image repository (default: tonyooo/ai-dev-env)
  BROWSER_REGISTRY  Browser image repository (default: tonyooo/ai-dev-browser)
  VERSION           Exact version for local builds (default: latest)
  RELEASE_SERIES    Automatic release major.minor (default: 0.1)
  RELEASE_BASE      Base commit used to count main commits (default: root)
  IMAGE_GIT_REMOTE  Git remote containing main (default: origin)
  AI_DEV_VERSION_STATE_DIR  Local release lock directory
  SKIP_VENDOR=1     Do not pre-download vendor/blobs/ before building

Image tags:
  $REGISTRY:<version>-<arch> and $REGISTRY:latest-<arch>
  $BROWSER_REGISTRY:<version>-<arch> and :latest-<arch> (with --with-browser)
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --push)
            PUSH=1
            shift
            ;;
        --with-browser)
            WITH_BROWSER=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

ARCH=$(host_arch) || { echo "error: unsupported host arch: $(uname -m)" >&2; exit 1; }

trap finish_build EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ "$PUSH" -eq 1 ]; then
    [ -z "$VERSION" ] || die 'VERSION cannot be set for --push; release versions are generated automatically'
    command -v gh >/dev/null 2>&1 || die 'GitHub CLI (gh) is required to trigger the multi-architecture release'
    gh auth status >/dev/null 2>&1 || die 'authenticate GitHub CLI before publishing'
    reserve_version
else
    VERSION=${VERSION:-latest}
    [[ "$VERSION" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,110}$ ]] || die 'invalid image VERSION'
fi

# Large build-time payloads are installed from vendor/blobs/ when present and
# verified; missing ones only fall back to the network. Fetching them up front
# once is what keeps a slow or flaky link from aborting the build mid-way.
# Only this machine's architecture is fetched, so nothing for another arch is
# downloaded.
if [ "${SKIP_VENDOR:-0}" != "1" ]; then
    if ! bash "$ROOT/scripts/vendor.sh"; then
        echo "warning: vendor payloads incomplete; the build will download the rest" >&2
    fi
fi

tag="${REGISTRY}:${VERSION}"
[ "$PUSH" -eq 0 ] || tag="${tag}-${ARCH}"
LABEL_ARGS=(--label "org.opencontainers.image.version=${VERSION}")
if [ "$PUSH" -eq 1 ]; then
    LABEL_ARGS+=(--label "org.opencontainers.image.revision=${REVISION}")
fi

if [ "$PUSH" -eq 0 ] || [ "$DEV_STATE" = new ]; then
    echo "==> building ${tag} (linux/${ARCH})"
    docker build \
        -f "$BUILD_FILE" \
        --platform "linux/${ARCH}" \
        --build-arg "TARGETARCH=${ARCH}" \
        "${LABEL_ARGS[@]}" \
        -t "$tag" \
        "$BUILD_CONTEXT"
    echo "==> built ${tag}"
else
    echo "==> using published ${tag}"
fi

if [ "$WITH_BROWSER" -eq 1 ]; then
    browser_tag="${BROWSER_REGISTRY}:${VERSION}"
    [ "$PUSH" -eq 0 ] || browser_tag="${browser_tag}-${ARCH}"
    if [ "$PUSH" -eq 0 ] || [ "$BROWSER_STATE" = new ]; then
        echo "==> building ${browser_tag} (linux/${ARCH})"
        docker build \
            -f "$(host_path "$ROOT/browser/Dockerfile")" \
            --platform "linux/${ARCH}" \
            "${LABEL_ARGS[@]}" \
            -t "$browser_tag" \
            "$(host_path "$ROOT")"
        echo "==> built ${browser_tag}"
    else
        echo "==> using published ${browser_tag}"
    fi
fi

if [ "$PUSH" -eq 1 ]; then
    check_release_source
    if [ "$DEV_STATE" = new ]; then
        echo "==> pushing ${tag}"
        docker push "$tag"
    fi
    if [ "$WITH_BROWSER" -eq 1 ] && [ "$BROWSER_STATE" = new ]; then
        echo "==> pushing ${browser_tag}"
        docker push "$browser_tag"
    fi
    # A new main commit or local edit during a slow push must not update latest.
    check_release_source
    # Historical releases must not move the latest aliases backwards.
    if [ "$IS_LATEST" -eq 1 ]; then
        docker tag "$tag" "${REGISTRY}:latest-${ARCH}"
        if [ "$WITH_BROWSER" -eq 1 ]; then
            docker tag "$browser_tag" "${BROWSER_REGISTRY}:latest-${ARCH}"
        fi
        docker push "${REGISTRY}:latest-${ARCH}"
        if [ "$WITH_BROWSER" -eq 1 ]; then
            docker push "${BROWSER_REGISTRY}:latest-${ARCH}"
        fi
    else
        echo '==> historical main revision: latest aliases unchanged'
    fi
    echo '==> requesting multi-architecture release check'
    (cd "$ROOT" && gh workflow run image-release.yml --ref main \
        -f "version=$VERSION" -f "revision=$REVISION" \
        -f "base=$RELEASE_BASE" -f "series=$RELEASE_SERIES" \
        -f "registry=$REGISTRY" -f "browser_registry=$BROWSER_REGISTRY") \
        || die 'images were pushed but the release check could not be triggered; retry this command'
fi

echo "==> done"
