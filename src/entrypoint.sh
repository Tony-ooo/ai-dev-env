#!/usr/bin/env bash
set -euo pipefail

# Container stage 0: lay out the data volume, validate service definitions, then
# hand over to /init. The image starts as dev (so docker exec lands there), but
# volume layout and s6 need root, so a setuid wrapper re-executes in place and
# /init stays PID 1.

readonly DATA_DIR=/home/dev/.ai-data
readonly S6_RC=/etc/s6-overlay/s6-rc.d
readonly S6_SKEL=/opt/ai-dev-env/s6-skel
readonly CONFIG_STATE=/var/lib/ai-dev-env/home-config-initialized
CONFIGS_INITIALIZED=0

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# Replace $1 with a symlink to $2, first folding any existing content at $1 into
# the volume. Some tools replace configs atomically (write temp + rename), which
# turns the symlink into a regular file; recovering both file and directory
# cases keeps user data from silently staying in the container layer.
link() {
    local src=$1 dst=$2

    if [ -d "$src" ] && [ ! -L "$src" ]; then
        mkdir -p "$dst"
        if ! cp -a "$src"/. "$dst"/; then
            log "cannot migrate $src; original data retained"
            return 1
        fi
        rm -rf "$src"
        log "merged directory into volume: $src"
    elif [ -e "$src" ] && [ ! -L "$src" ]; then
        if ! cp -af "$src" "$dst"; then
            log "cannot migrate $src; original data retained"
            return 1
        fi
        log "recovered file into volume: $src"
        rm -f "$src"
    else
        rm -f "$src"
    fi

    ln -s "$dst" "$src"
}

# A new container seeds only missing files, including when the saved file is empty.
# On later boots of the same container, a regular file replaced the established
# symlink and is therefore a user edit, not an image default.
seed_file() {
    local src=$1 dst=$2

    if [ ! -L "$src" ] && [ -f "$src" ] && \
       { [ "$CONFIGS_INITIALIZED" -eq 1 ] || [ ! -e "$dst" ]; }; then
        if ! cp -af "$src" "$dst"; then
            log "cannot persist $src; original file retained"
            return 1
        fi
        log "persisted file: $src"
    fi

    rm -f "$src"
    ln -s "$dst" "$src"
}

seed_s6_source() {
    local template=$1 destination=$2 directory name item
    for directory in "$template"/*; do
        [ -d "$directory" ] || continue
        name=$(basename "$directory")
        if [ -f "$destination/$name/contents" ] && [ ! -d "$destination/$name/contents.d" ]; then
            # An empty contents.d from the image would shadow the user's
            # legacy contents file before the service guard can inspect it.
            for item in "$directory"/*; do
                [ -e "$item" ] || [ -L "$item" ] || continue
                [ "$(basename "$item")" != contents.d ] || continue
                cp -an "$item" "$destination/$name"/ || return 1
            done
        else
            cp -an "$directory" "$destination"/ || return 1
        fi
    done
}

prepare_volume() {
    if [ ! -d "$DATA_DIR" ] || [ -L "$DATA_DIR" ]; then
        log "warning: no volume mounted at $DATA_DIR — changes will NOT persist"
        return 0
    fi

    # Create directories as dev up front, so no recursive chown is needed at boot.
    for d in workspace .claude .codex .vscode-server home-config s6-services ssh agents \
             browser browser/downloads browser/profile browser/state; do
        install -d -o dev -g dev "$DATA_DIR/$d"
    done
    # Built-in skills directory (~/.agents/skills).
    install -d -o dev -g dev "$DATA_DIR/agents/skills"
    install -d -o dev -g dev "$DATA_DIR/home-config/.config"
    chmod 0700 "$DATA_DIR/ssh"

    # s6 definitions: idempotently seed from the image skeleton on every boot
    # (cp -an never overwrites user definitions, while new built-ins still land).
    if [ -d "$S6_SKEL" ]; then
        seed_s6_source "$S6_SKEL" "$DATA_DIR/s6-services"
    fi
    if [ ! -L "$S6_RC" ]; then
        rm -rf "$S6_RC"
        ln -s "$DATA_DIR/s6-services" "$S6_RC"
    fi

    persist_home

    # Single-file sources get the right owner at creation time; later > redirects
    # only truncate and do not change ownership.
    ensure_file "$DATA_DIR/home-config/claude.json" '{"hasCompletedOnboarding":true}'
    ensure_file "$DATA_DIR/home-config/bashrc.extra" ''
    ensure_file "$DATA_DIR/home-config/gitconfig" ''
    ensure_file "$DATA_DIR/home-config/npmrc" 'prefix=/home/dev/.npm-global'
    install -d -m 0755 "$(dirname "$CONFIG_STATE")"
    touch "$CONFIG_STATE"
    chmod 0644 "$CONFIG_STATE"
}

persist_home() {
    link /home/dev/workspace      "$DATA_DIR/workspace"
    link /home/dev/downloads      "$DATA_DIR/browser/downloads"
    link /home/dev/.claude        "$DATA_DIR/.claude"
    link /home/dev/.codex         "$DATA_DIR/.codex"
    link /home/dev/.vscode-server "$DATA_DIR/.vscode-server"
    link /home/dev/.ssh           "$DATA_DIR/ssh"
    link /home/dev/.agents        "$DATA_DIR/agents"
    seed_file /home/dev/.claude.json  "$DATA_DIR/home-config/claude.json"
    seed_file /home/dev/.bashrc.extra "$DATA_DIR/home-config/bashrc.extra"
    link      /home/dev/.config       "$DATA_DIR/home-config/.config"
    seed_file /home/dev/.gitconfig    "$DATA_DIR/home-config/gitconfig"
    seed_file /home/dev/.npmrc        "$DATA_DIR/home-config/npmrc"
}

ensure_file() {
    local path=$1 content=$2
    if [ -L "$path" ]; then
        path=$(readlink -f "$path")
    fi
    if [ ! -f "$path" ]; then
        install -o dev -g dev -m 0644 /dev/null "$path"
        printf '%s' "$content" > "$path"
    fi
}

# Claude Code scans only ~/.claude/skills, while built-in and user skills both
# live in ~/.agents/skills. Re-assert the link on every boot: if a tool made
# it a real directory or repointed it, link() merges the contents back into
# the target first, so skills are not lost across a rebuild.
assert_skills_link() {
    install -d -o dev -g dev /home/dev/.claude /home/dev/.agents/skills
    link /home/dev/.claude/skills /home/dev/.agents/skills
}
main() {
    # The working directory may be replaced with a symlink during migration.
    cd /
    if [ "$(id -u)" -ne 0 ]; then
        exec /usr/local/bin/root-exec "$@"
    fi
    [ ! -f "$CONFIG_STATE" ] || CONFIGS_INITIALIZED=1
    if [ "${1:-}" = --persist-home ]; then
        if [ "$CONFIGS_INITIALIZED" -ne 1 ] || [ ! -d "$DATA_DIR" ] || [ -L "$DATA_DIR" ]; then
            log "home is not initialized; refusing pre-recreate persistence"
            return 1
        fi
        persist_home
        assert_skills_link
        return
    fi
    prepare_volume
    assert_skills_link
    /usr/local/bin/s6-guard.sh
    exec /init "$@"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
