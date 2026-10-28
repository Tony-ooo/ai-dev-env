#!/command/with-contenv bash
# shellcheck shell=bash
set -euo pipefail

# s6 cont-init.d hook: runs after /init builds the supervision tree and before
# the services start. The with-contenv interpreter is required because these
# hooks inherit s6-supervise's environment, while container variables
# (USER_PASSWORD / SSH_PORT / HOST_UID) live in /run/s6/container_environment.

DATA_DIR=/home/dev/.ai-data
SSH_PORT=${SSH_PORT:-22}

log() { printf '[sysinit] %s\n' "$*"; }

# --- 1. Optional host UID/GID remap ---
# Unnecessary with a named volume; pass HOST_UID/HOST_GID only when bind-mounting
# a host directory.
remap_dev() {
    local current_uid current_gid target_uid target_gid value status
    [ -n "${HOST_UID:-}${HOST_GID:-}" ] || return 0
    current_uid=$(id -u dev)
    current_gid=$(id -g dev)
    target_uid=${HOST_UID:-$current_uid}
    target_gid=${HOST_GID:-${HOST_UID:-$current_gid}}
    for value in "$target_uid" "$target_gid"; do
        if [[ ! "$value" =~ ^[1-9][0-9]{0,9}$ ]] || [ "$value" -gt 2147483647 ]; then
            log "HOST_UID and HOST_GID must be positive numeric IDs below 2147483648"
            return 1
        fi
    done
    if [ "$target_uid" = "$current_uid" ] && [ "$target_gid" = "$current_gid" ]; then
        return 0
    fi
    # Check the UID before groupmod, so a known user collision cannot leave
    # the group half-remapped when usermod subsequently rejects the UID.
    if [ "$target_uid" != "$current_uid" ]; then
        if getent passwd "$target_uid" >/dev/null; then
            log "HOST_UID is already assigned to another user"
            return 1
        else
            status=$?
            [ "$status" -eq 2 ] || { log "cannot check HOST_UID availability"; return 1; }
        fi
    fi
    log "remapping dev -> ${target_uid}:${target_gid} (this will chown the volume)"
    if [ "$target_gid" != "$current_gid" ]; then
        groupmod -g "$target_gid" dev || return 1
    fi
    usermod -u "$target_uid" -g "$target_gid" dev || return 1
    # usermod updates UID-owned home files, but a GID-only change also needs
    # their group ownership repaired. The data volume is inside this home;
    # traverse it only once, and only on an explicit remap.
    chown -R dev:dev /home/dev || return 1
}

if [ "${BASH_SOURCE[0]}" != "$0" ]; then
    return 0
fi
remap_dev

# --- 2. User password ---
if [ -n "${USER_PASSWORD:-}" ]; then
    printf 'dev:%s\n' "$USER_PASSWORD" | chpasswd
    log "password set for dev"
else
    log "warning: USER_PASSWORD unset — SSH password login will fail"
fi

# --- 3. SSH ----------------------------------------------------------------
# Host keys live in the data volume: per-user, stable across rebuilds.
# The image ships none, so no two containers share a private key.
KEYDIR=/etc/ssh
if [ -d "$DATA_DIR" ] && [ ! -L "$DATA_DIR" ]; then
    KEYDIR=$DATA_DIR/ssh
fi

for t in ed25519 rsa; do
    key=$KEYDIR/ssh_host_${t}_key
    if [ ! -f "$key" ]; then
        log "generating SSH host key: $t"
        ssh-keygen -q -t "$t" -N '' -C "ai-dev-$(hostname)" -f "$key"
    fi
    if [ "$KEYDIR" != /etc/ssh ]; then
        ln -sfn "$key"     "/etc/ssh/ssh_host_${t}_key"
        ln -sfn "$key.pub" "/etc/ssh/ssh_host_${t}_key.pub"
    fi
done

chown root:root "$KEYDIR"/ssh_host_*_key 2>/dev/null || true
chmod 0600 "$KEYDIR"/ssh_host_*_key 2>/dev/null || true

mkdir -p /run/sshd
sed -i "s/^#\?Port .*/Port ${SSH_PORT}/" /etc/ssh/sshd_config

# --- 4. Data volume ownership ---
# Directories and config files are already owned by dev (the entrypoint creates
# them); this is a non-recursive safety net. Only the entries themselves are
# fixed: workspace and .vscode-server may hold hundreds of thousands of files,
# so a full scan on every boot would cost far more than it is worth.
if [ -d "$DATA_DIR" ] && [ ! -L "$DATA_DIR" ]; then
    chown dev:dev /home/dev "$DATA_DIR" 2>/dev/null || true

    for d in workspace home-config s6-services ssh agents .claude .codex .vscode-server browser; do
        if [ -d "$DATA_DIR/$d" ]; then
            chown dev:dev "$DATA_DIR/$d" 2>/dev/null || true
        fi
    done

    # Service definitions are few, so a recursive fix here is cheap.
    if [ -d "$DATA_DIR/s6-services" ]; then
        chown -R dev:dev "$DATA_DIR/s6-services" 2>/dev/null || true
    fi

    chown dev:dev "$DATA_DIR"/home-config/* 2>/dev/null || true
fi

# --- 5. Built-in skills ---
# Skills live in ~/.agents/skills, inside the data volume, so they survive rebuilds.
# The entrypoint re-asserts ~/.claude/skills as a symlink to it on every boot;
# .codex gets no skills directory and no link.
SKILLS_DIR=/home/dev/.agents/skills
if [ -d /opt/ai-dev-env/skills ]; then
    install -d -o dev -g dev "$SKILLS_DIR"
    cp -a /opt/ai-dev-env/skills/. "$SKILLS_DIR"/
    chown -R dev:dev "$SKILLS_DIR"
    log "built-in skills synced to ~/.agents/skills"
fi

# --- 6. Workspace welcome document ---
if [ -f /opt/ai-dev-env/welcome.md ] && [ -d /home/dev/workspace ] \
   && [ ! -e /home/dev/workspace/README.md ]; then
    cp /opt/ai-dev-env/welcome.md /home/dev/workspace/README.md
    chown dev:dev /home/dev/workspace/README.md
fi

# --- 7. Configuration self-check ---
/usr/sbin/sshd -t
log "init complete (ssh port ${SSH_PORT})"
