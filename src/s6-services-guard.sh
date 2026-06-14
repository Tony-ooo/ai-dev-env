#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR=${S6_RC_SOURCE_DIR:-/etc/s6-overlay/s6-rc.d}
DISABLED_ROOT="$SOURCE_DIR/.disabled-by-guard"

log() {
    printf '[s6-services-guard] %s\n' "$*" >&2
}

first_line() {
    local file_path=$1
    local line=""

    if [ -f "$file_path" ]; then
        IFS= read -r line < "$file_path" || true
        line=${line%$'\r'}
    fi

    printf '%s' "$line"
}

ensure_bundle() {
    local bundle=$1
    local bundle_dir="$SOURCE_DIR/$bundle"
    local type_file="$bundle_dir/type"

    mkdir -p "$bundle_dir/contents.d"
    if [ "$(first_line "$type_file")" != "bundle" ]; then
        printf 'bundle\n' > "$type_file"
        chmod 0644 "$type_file"
        log "restored bundle: $bundle"
    fi
}

next_disabled_path() {
    local name=$1
    local stamp
    local candidate
    local index=0

    stamp=$(date -u '+%Y%m%dT%H%M%SZ' 2>/dev/null || printf 'unknown')
    candidate="$DISABLED_ROOT/${name}.${stamp}"

    while [ -e "$candidate" ]; do
        index=$((index + 1))
        candidate="$DISABLED_ROOT/${name}.${stamp}.${index}"
    done

    printf '%s' "$candidate"
}

remove_bundle_entry() {
    local name=$1

    rm -f "$SOURCE_DIR/user/contents.d/$name" "$SOURCE_DIR/user2/contents.d/$name" 2>/dev/null || true
}

disable_service_dir() {
    local service_dir=$1
    local reason=$2
    local name
    local target

    name=$(basename "$service_dir")
    mkdir -p "$DISABLED_ROOT"
    target=$(next_disabled_path "$name")

    remove_bundle_entry "$name"
    if mv "$service_dir" "$target"; then
        {
            printf 'disabled_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || printf 'unknown')"
            printf 'reason=%s\n' "$reason"
        } > "$target/disabled-reason.txt"
        log "disabled service '$name': $reason"
    else
        log "warning: failed to disable service '$name'"
    fi
}

parse_run_workdir() {
    local run_file=$1
    local line
    local path

    while IFS= read -r line; do
        line=${line%$'\r'}
        line="${line#"${line%%[![:space:]]*}"}"

        case "$line" in
            cd\ --\ *) path=${line#cd -- } ;;
            cd\ *) path=${line#cd } ;;
            *) continue ;;
        esac

        case "$path" in
            /*[[:space:]]*) return 1 ;;
            /*)
                printf '%s' "$path"
                return 0
                ;;
            \'/*\')
                path=${path#\'}
                path=${path%\'}
                case "$path" in
                    *\'*) return 1 ;;
                    *)
                        printf '%s' "$path"
                        return 0
                        ;;
                esac
                ;;
            \"/*\")
                path=${path#\"}
                path=${path%\"}
                case "$path" in
                    *\"*) return 1 ;;
                    *)
                        printf '%s' "$path"
                        return 0
                        ;;
                esac
                ;;
        esac
    done < "$run_file"

    return 1
}

validate_service_dir() {
    local service_dir=$1
    local name
    local type_file="$service_dir/type"
    local service_type
    local run_file="$service_dir/run"
    local workdir

    name=$(basename "$service_dir")

    case "$name" in
        [A-Za-z0-9]*)
            ;;
        *)
            disable_service_dir "$service_dir" "invalid service name"
            return
            ;;
    esac

    case "$name" in
        *[!A-Za-z0-9_.-]*)
            disable_service_dir "$service_dir" "invalid service name"
            return
            ;;
    esac

    if [ ! -f "$type_file" ]; then
        disable_service_dir "$service_dir" "missing type file"
        return
    fi

    service_type=$(first_line "$type_file")
    case "$service_type" in
        longrun)
            if [ ! -f "$run_file" ]; then
                disable_service_dir "$service_dir" "missing run script"
                return
            fi

            if [ ! -x "$run_file" ]; then
                chmod 0755 "$run_file" || true
                log "restored executable bit: $name/run"
            fi

            if workdir=$(parse_run_workdir "$run_file"); then
                if [ -n "$workdir" ] && [ ! -d "$workdir" ]; then
                    disable_service_dir "$service_dir" "run workdir does not exist"
                fi
            fi
            ;;
        oneshot)
            if [ ! -f "$service_dir/up" ] && [ ! -f "$service_dir/down" ]; then
                disable_service_dir "$service_dir" "oneshot has no up or down script"
            fi
            ;;
        bundle)
            mkdir -p "$service_dir/contents.d"
            ;;
        *)
            disable_service_dir "$service_dir" "unsupported service type"
            ;;
    esac
}

remove_orphan_bundle_entries() {
    local bundle
    local entry
    local name

    for bundle in user user2; do
        [ -d "$SOURCE_DIR/$bundle/contents.d" ] || continue

        for entry in "$SOURCE_DIR/$bundle/contents.d"/*; do
            [ -e "$entry" ] || continue
            [ -f "$entry" ] || continue

            name=$(basename "$entry")
            if [ ! -d "$SOURCE_DIR/$name" ]; then
                rm -f "$entry"
                log "removed orphan bundle entry: $bundle/contents.d/$name"
            fi
        done
    done
}

compile_check() {
    local tmpdir
    local rc=0

    [ -x /command/s6-rc-compile ] || return 0

    tmpdir=$(mktemp -d)
    if ! /command/s6-rc-compile "$tmpdir/compiled" "$SOURCE_DIR" >/dev/null 2>&1; then
        rc=1
    fi
    rm -rf "$tmpdir"

    return "$rc"
}

disable_all_custom_services() {
    local service_dir
    local name

    for service_dir in "$SOURCE_DIR"/*; do
        [ -d "$service_dir" ] || continue
        name=$(basename "$service_dir")

        case "$name" in
            user|user2)
                ;;
            *)
                disable_service_dir "$service_dir" "s6-rc compile validation failed"
                ;;
        esac
    done

    rm -f "$SOURCE_DIR/user/contents.d/"* "$SOURCE_DIR/user2/contents.d/"* 2>/dev/null || true
}

main() {
    local service_dir
    local name

    case "${S6_SERVICES_GUARD:-1}" in
        0|false|FALSE|no|NO)
            log "disabled by S6_SERVICES_GUARD"
            return 0
            ;;
    esac

    mkdir -p "$SOURCE_DIR"
    ensure_bundle user
    ensure_bundle user2

    for service_dir in "$SOURCE_DIR"/*; do
        [ -d "$service_dir" ] || continue
        name=$(basename "$service_dir")

        case "$name" in
            user|user2)
                ;;
            *)
                validate_service_dir "$service_dir"
                ;;
        esac
    done

    remove_orphan_bundle_entries

    if ! compile_check; then
        log "warning: compile validation failed; disabling custom services"
        disable_all_custom_services
        ensure_bundle user
        ensure_bundle user2
        remove_orphan_bundle_entries
    fi

    if ! compile_check; then
        log "warning: compile validation still fails after guard"
    fi
}

main "$@"
