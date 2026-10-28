#!/usr/bin/env bash
set -euo pipefail

# s6 service definition validation: a broken definition must not block container
# startup. Runs before /init: every definition in the data volume is checked, the
# ones that cannot start are moved to .disabled-by-guard/ with a reason, and the
# result is test-compiled with s6-rc-compile. If compilation fails, the compilable
# set is rebuilt from the image defaults while keeping valid custom closures.
# Disable with S6_SERVICES_GUARD=0.

SRC=${S6_RC_SOURCE_DIR:-/etc/s6-overlay/s6-rc.d}
BASE=${S6_RC_BASE_DIR:-/opt/ai-dev-env/s6-skel}
DISABLED=$SRC/.disabled-by-guard
BUNDLES="user user2"

log() { printf '[s6-guard] %s\n' "$*" >&2; }

first_line() {
    local v=""
    if [ -f "$1" ]; then
        IFS= read -r v < "$1" || true
    fi
    printf '%s' "${v%$'\r'}"
}

ensure_bundle() {
    local b=$1
    install -d "$SRC/$b"
    if [ ! -d "$SRC/$b/contents.d" ] && [ ! -f "$SRC/$b/contents" ]; then
        install -d "$SRC/$b/contents.d"
    fi
    if [ "$(first_line "$SRC/$b/type")" != "bundle" ]; then
        printf 'bundle\n' > "$SRC/$b/type"
        log "restored bundle: $b"
    fi
}

# Quarantine a service directory and drop its references from the bundles.
disable() {
    local dir=$1 reason=$2 name target b

    name=$(basename "$dir")
    install -d "$DISABLED"
    target="$DISABLED/$name.$(date -u +%Y%m%dT%H%M%SZ)"
    if [ -e "$target" ]; then
        target="$target.$$"
    fi

    for b in $BUNDLES; do
        rm -f "$SRC/$b/contents.d/$name" 2>/dev/null || true
    done

    mv "$dir" "$target" || return 1
    printf 'disabled_at=%s\nreason=%s\n' "$(date -u +%FT%TZ)" "$reason" \
        > "$target/disabled-reason.txt"
    log "disabled '$name': $reason"
}

validate() {
    local dir=$1 name type

    name=$(basename "$dir")

    case "$name" in
        [A-Za-z0-9]*) ;;
        *) disable "$dir" "invalid service name"; return ;;
    esac
    case "$name" in
        *[!A-Za-z0-9_.-]*) disable "$dir" "invalid service name"; return ;;
    esac

    if [ ! -f "$dir/type" ]; then
        disable "$dir" "missing type file"
        return
    fi

    type=$(first_line "$dir/type")
    case "$type" in
        longrun)
            if [ ! -f "$dir/run" ]; then
                disable "$dir" "missing run script"
                return
            fi
            if [ ! -x "$dir/run" ]; then
                chmod 0755 "$dir/run" 2>/dev/null || true
                log "restored exec bit: $name/run"
            fi
            ;;
        oneshot)
            if [ ! -f "$dir/up" ]; then
                disable "$dir" "oneshot has no up command"
            fi
            ;;
        bundle)
            if [ ! -d "$dir/contents.d" ] && [ ! -f "$dir/contents" ]; then
                install -d "$dir/contents.d"
            fi
            ;;
        *)
            disable "$dir" "unsupported type: $type"
            ;;
    esac
}

# Dangling bundle entries that point at a service which no longer exists.
prune_orphans() {
    local b entry name

    for b in $BUNDLES; do
        [ -d "$SRC/$b/contents.d" ] || continue
        for entry in "$SRC/$b/contents.d"/*; do
            [ -e "$entry" ] || continue
            name=$(basename "$entry")
            if [ ! -d "$SRC/$name" ]; then
                rm -f "$entry"
                log "pruned orphan entry: $b/$name"
            fi
        done
    done
}

compile_ok() {
    local source=${1:-$SRC} tmp rc=0

    tmp=$(mktemp -d) || return 1
    /command/s6-rc-compile "$tmp/db" "$source" >"$tmp/diagnostic" 2>&1 || rc=1
    if [ "$rc" -ne 0 ]; then
        cat "$tmp/diagnostic" >&2
    fi
    rm -rf "$tmp"
    return "$rc"
}

# Emit both current and legacy dependency/bundle references.
references() {
    local dir=$1 field entry
    field=dependencies
    [ "$(first_line "$dir/type")" != bundle ] || field=contents
    if [ -d "$dir/$field.d" ]; then
        for entry in "$dir/$field.d"/*; do
            [ -e "$entry" ] || [ -L "$entry" ] || continue
            basename "$entry"
        done
    elif [ -f "$dir/$field" ]; then
        sed -e 's/^[[:space:]]*//' -e '/^#/d' -e '/^$/d' "$dir/$field"
    fi
}

add_closure() {
    local name=$1 target=$2 reference
    case "$name" in
        ''|[!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*) return 1 ;;
    esac
    [ ! -d "$target/$name" ] || return 0
    [ -d "$SRC/$name" ] || return 1
    cp -a "$SRC/$name" "$target/$name" || return 1
    while IFS= read -r reference || [ -n "$reference" ]; do
        add_closure "$reference" "$target" || return 1
    done < <(references "$SRC/$name")
}

# Validate dependency closures in scratch space before changing the live source.
# A bad unreferenced service must not make us discard unrelated healthy services.
recover_compilable_source() (
    local tmp dir name b reference
    [ -d "$BASE" ] || { log "missing base definitions: $BASE"; return 1; }
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    mkdir "$tmp/good"
    cp -a "$BASE"/. "$tmp/good"/
    for b in $BUNDLES; do
        # Preserve legacy bundle contents before switching this scratch copy
        # to individual membership files for incremental validation.
        if [ ! -d "$tmp/good/$b/contents.d" ]; then
            references "$tmp/good/$b" > "$tmp/$b.members"
            mkdir -p "$tmp/good/$b/contents.d"
            while IFS= read -r reference || [ -n "$reference" ]; do
                case "$reference" in ''|[!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*) return 1 ;; esac
                touch "$tmp/good/$b/contents.d/$reference"
            done < "$tmp/$b.members"
        fi
        printf 'bundle\n' > "$tmp/good/$b/type"
    done
    compile_ok "$tmp/good" || { log "base definitions do not compile"; return 1; }

    # Prefer valid user changes to base services, even if an unrelated custom
    # definition forced recovery. Keep the base version if the candidate fails.
    for dir in "$SRC"/*; do
        [ -d "$dir" ] || continue
        name=$(basename "$dir")
        case "$name" in user|user2) continue ;; esac
        [ -d "$tmp/good/$name" ] || continue
        cp -a "$tmp/good" "$tmp/probe"
        rm -rf "$tmp/probe/$name"
        if add_closure "$name" "$tmp/probe" && compile_ok "$tmp/probe"; then
            rm -rf "$tmp/good"
            mv "$tmp/probe" "$tmp/good"
        else
            rm -rf "$tmp/probe"
        fi
    done

    for dir in "$SRC"/*; do
        [ -d "$dir" ] || continue
        name=$(basename "$dir")
        [ ! -d "$tmp/good/$name" ] || continue
        cp -a "$tmp/good" "$tmp/probe"
        if add_closure "$name" "$tmp/probe" && compile_ok "$tmp/probe"; then
            rm -rf "$tmp/good"
            mv "$tmp/probe" "$tmp/good"
        else
            rm -rf "$tmp/probe"
        fi
    done
    for b in $BUNDLES; do
        while IFS= read -r reference || [ -n "$reference" ]; do
            case "$reference" in
                ''|[!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*) continue ;;
            esac
            if [ -d "$tmp/good/$reference" ]; then
                [ ! -e "$tmp/good/$b/contents.d/$reference" ] || continue
                touch "$tmp/good/$b/contents.d/$reference"
                if ! compile_ok "$tmp/good"; then
                    rm -f "$tmp/good/$b/contents.d/$reference"
                    log "removed invalid bundle membership: $b -> $reference"
                fi
            fi
        done < <(references "$SRC/$b")
    done
    compile_ok "$tmp/good" || return 1

    for dir in "$SRC"/*; do
        [ -d "$dir" ] || continue
        name=$(basename "$dir")
        if [ ! -d "$tmp/good/$name" ]; then
            disable "$dir" "definition or dependency closure failed compilation"
        elif ! diff -qr "$dir" "$tmp/good/$name" >/dev/null; then
            disable "$dir" "restored validated base definition"
        fi
    done
    # Copy the prevalidated set, including restored bundle membership. The
    # archive is dot-prefixed and is intentionally ignored by s6-rc-compile.
    cp -a "$tmp/good"/. "$SRC"/
    compile_ok
)

main() {
    local dir name b

    case "${S6_SERVICES_GUARD:-1}" in
        0|false|FALSE|no|NO)
            log "disabled by S6_SERVICES_GUARD"
            return 0
            ;;
    esac

    [ -x /command/s6-rc-compile ] || { log "s6-rc-compile is unavailable"; return 1; }

    install -d "$SRC"
    for b in $BUNDLES; do
        ensure_bundle "$b"
    done

    for dir in "$SRC"/*; do
        [ -d "$dir" ] || continue
        name=$(basename "$dir")
        case "$name" in
            user|user2|.disabled-by-guard) continue ;;
        esac
        if [ ! -d "$BASE/$name" ]; then
            validate "$dir"
        fi
    done

    prune_orphans

    if ! compile_ok; then
        log "compile validation failed; recovering a validated service set"
        recover_compilable_source
    fi
}

main "$@"
