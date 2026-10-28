#!/usr/bin/env bash
# shellcheck shell=bash
# Shared helpers for the management scripts (sourced, never executed directly).

# Container paths must not be rewritten by Git Bash. Host paths are converted
# explicitly with host_path before being passed to Docker.
export MSYS_NO_PATHCONV=1

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

USER_LOCK_ID=""
acquire_user_lock() {
    local user=$1 image=$2
    USER_LOCK_NAME="ai-dev-${user}-operation-lock"
    if ! USER_LOCK_ID=$(docker create --name "$USER_LOCK_NAME" --network none \
        --read-only --label ai-dev-env.managed=true \
        --label "ai-dev-env.user=$user" --label ai-dev-env.role=operation-lock \
        --entrypoint /bin/true "$image" 2>/dev/null); then
        USER_LOCK_ID=""
        if docker inspect "$USER_LOCK_NAME" >/dev/null 2>&1; then
            die "another operation holds $USER_LOCK_NAME; if interrupted, inspect the lock before removing it"
        fi
        die "could not create operation lock; check Docker and helper image: $image"
    fi
}

release_user_lock() {
    if [ -n "$USER_LOCK_ID" ]; then
        docker rm "$USER_LOCK_ID" >/dev/null \
            || { printf 'error: could not release operation lock: %s\n' "$USER_LOCK_NAME" >&2; return 1; }
        USER_LOCK_ID=""
    fi
}

finish_locked_operation() {
    local code=$?
    trap - EXIT INT TERM
    release_user_lock || code=1
    exit "$code"
}

host_arch() {
    case "$(uname -m)" in
        x86_64)        printf 'amd64' ;;
        aarch64|arm64) printf 'arm64' ;;
        *)             return 1 ;;
    esac
}

check_image_release_pair() {
    local dev_image=$1 browser_image=$2 expected_version=${3:-} key dev_value browser_value
    for key in org.opencontainers.image.version org.opencontainers.image.revision; do
        dev_value=$(docker image inspect -f "{{index .Config.Labels \"$key\"}}" "$dev_image") \
            || die "cannot inspect dev image release: $dev_image"
        browser_value=$(docker image inspect -f "{{index .Config.Labels \"$key\"}}" "$browser_image") \
            || die "cannot inspect browser image release: $browser_image"
        if [ -n "$dev_value" ] && [ "$dev_value" != '<no value>' ] && \
           [ -n "$browser_value" ] && [ "$browser_value" != '<no value>' ] && \
           [ "$dev_value" != "$browser_value" ]; then
            die "dev and browser images have different releases ($key mismatch); use matching VERSION tags"
        fi
        if [ "$key" = org.opencontainers.image.version ] && [ -n "$expected_version" ]; then
            if { [ -n "$dev_value" ] && [ "$dev_value" != '<no value>' ] && [ "$dev_value" != "$expected_version" ]; } || \
               { [ -n "$browser_value" ] && [ "$browser_value" != '<no value>' ] && [ "$browser_value" != "$expected_version" ]; }; then
                die "image release label does not match VERSION=$expected_version"
            fi
        fi
    done
}

# User name rule: starts with a letter or digit; then letters, digits, dot,
# underscore or hyphen.
valid_user_name() {
    case "$1" in
        ''|[!A-Za-z0-9]*) return 1 ;;
        *[!A-Za-z0-9_.-]*) return 1 ;;
    esac
    return 0
}

# Convert a path into a host path Docker accepts (Git Bash/MSYS path mangling).
host_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -m "$1"
    else
        printf '%s' "$1"
    fi
}

port_base() {
    case "$1" in
        ''|*[!0-9]*) die "port base must be numeric: $1" ;;
    esac
    [ "${#1}" -le 3 ] || die "port base must be between 1 and 655"
    local value=$((10#$1))
    [ "$value" -ge 1 ] && [ "$value" -le 655 ] \
        || die "port base must be between 1 and 655"
    printf '%s' "$value"
}

require_browser_docker() {
    local versions client server version major
    versions=$(docker version --format '{{.Client.Version}} {{.Server.Version}}') \
        || die "cannot query Docker client and engine"
    read -r client server <<< "$versions"
    for version in "$client" "$server"; do
        major=${version%%.*}
        case "$major" in ''|*[!0-9]*) die "cannot parse Docker version: $version" ;; esac
        [ "$major" -ge 26 ] || die "browser requires Docker CLI and Engine 26+ (found $client / $server)"
    done
}

ensure_network() {
    local network=$1 user=$2
    if ! docker network inspect "$network" >/dev/null 2>&1; then
        docker network create --label ai-dev-env.managed=true \
            --label "ai-dev-env.user=$user" "$network" >/dev/null \
            || die "cannot create network: $network"
    fi
}

connect_network() {
    local network=$1 container=$2 networks
    networks=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$container") \
        || die "cannot inspect networks for: $container"
    if ! printf '%s\n' "$networks" | grep -Fxq "$network"; then
        docker network connect "$network" "$container" \
            || die "cannot connect $container to $network"
    fi
}

wait_ready() {
    local container=$1 attempts=$2 i
    shift 2
    for ((i=0; i<attempts; i++)); do
        if docker exec "$container" "$@" >/dev/null 2>&1; then
            return 0
        fi
        if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" != true ]; then
            break
        fi
        sleep 1
    done
    printf 'error: container not ready: %s (inspect with docker logs %s)\n' "$container" "$container" >&2
    return 1
}
