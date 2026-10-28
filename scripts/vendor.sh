#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Pre-download build-time payloads into vendor/blobs/
# ============================================================
# The large payloads pulled during a build (npm native binaries, herdr, uv,
# s6-overlay) time out often on a weak link. This script fetches them as listed
# in vendor/manifest.txt; the Dockerfile then installs from the verified local
# copies offline instead of hitting the network.
#
# One download is reused by later builds; the install steps stay offline.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MANIFEST="$ROOT/vendor/manifest.txt"
BLOBS="$ROOT/vendor/blobs"
JOBS=${VENDOR_JOBS:-3}

usage() {
    cat <<'USAGE'
Usage: scripts/vendor.sh [options]

Download the build-time payloads listed in vendor/manifest.txt into
vendor/blobs/. Existing files with a matching checksum are kept.

Only payloads for this machine's architecture are fetched; the shared ones are
always included.

Options:
  --force       Re-download every payload even if the checksum matches.
  -h, --help    Show this help.

Env:
  VENDOR_JOBS  Parallel downloads (default: 3)

Blobs are not committed: they are ~640 MB for both architectures, above GitHub's
per-file limit, and CI caches them between builds instead.
USAGE
}

FORCE=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ARCH=$(host_arch) || { echo "error: unsupported host arch: $(uname -m)" >&2; exit 1; }

[ -f "$MANIFEST" ] || { echo "error: manifest not found: $MANIFEST" >&2; exit 1; }

command -v sha256sum >/dev/null 2>&1 \
    || { echo "error: sha256sum is required to verify downloads" >&2; exit 1; }
command -v curl >/dev/null 2>&1 \
    || { echo "error: curl is required to download payloads" >&2; exit 1; }

mkdir -p "$BLOBS"

checksum_of() {
    sha256sum "$1" | awk '{print $1}'
}

# Download one payload with resume and bounded retries, then verify the
# manifest checksum. A partial or mismatched file is removed so the next run
# starts clean instead of building from a corrupt blob.
fetch() {
    local want=$1 name=$2 url=$3
    local target="$BLOBS/$name" partial="$BLOBS/.$name.part"

    if [ "$FORCE" -eq 0 ] && [ -f "$target" ] && [ "$(checksum_of "$target")" = "$want" ]; then
        printf 'have   %s\n' "$name"
        return 0
    fi

    rm -f "$target"
    printf 'fetch  %s\n' "$name"
    # -C - resumes the partial file across retries, so a slow or interrupted
    # transfer does not restart from zero.
    if ! curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 \
            --connect-timeout 20 --max-time 1800 -C - -o "$partial" "$url"; then
        rm -f "$partial"
        printf 'error: download failed: %s\n' "$url" >&2
        return 1
    fi
    if [ "$(checksum_of "$partial")" != "$want" ]; then
        rm -f "$partial"
        printf 'error: checksum mismatch for %s (expected %s)\n' "$name" "$want" >&2
        return 1
    fi
    mv "$partial" "$target"
    printf 'saved  %s\n' "$name"
}

# Keep only the records that apply to the selected architectures: a record's 4th
# column is its own arch (amd64/arm64) or "any" for an arch-independent payload.
records=()
while IFS= read -r line; do
    records+=("$line")
done < <(grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$MANIFEST")

[ "${#records[@]}" -gt 0 ] || { echo "error: manifest lists no payloads" >&2; exit 1; }

selected=()
for record in "${records[@]}"; do
    read -r _want _name _url record_arch <<< "$record"
    [ -n "${record_arch:-}" ] \
        || { echo "error: manifest record has no arch column: $record" >&2; exit 1; }
    # "any" payloads contain no arch-specific binary, so they are always included.
    if [ "$record_arch" = any ] || [ "$record_arch" = "$ARCH" ]; then
        selected+=("$record")
    fi
done

[ "${#selected[@]}" -gt 0 ] || { echo "error: no payloads match arch: $ARCH" >&2; exit 1; }

printf '==> arch: %s (%d of %d payloads)\n' "$ARCH" "${#selected[@]}" "${#records[@]}"

# Run in fixed batches so a slow link still overlaps several transfers without
# needing a process pool; failures are collected and reported at the end.
failed=()
for ((i = 0; i < ${#selected[@]}; i += JOBS)); do
    pids=()
    for record in "${selected[@]:i:JOBS}"; do
        read -r want name url _arch <<< "$record"
        fetch "$want" "$name" "$url" &
        pids+=("$!:$name")
    done
    for entry in "${pids[@]}"; do
        pid=${entry%%:*}
        name=${entry#*:}
        wait "$pid" || failed+=("$name")
    done
done

if [ "${#failed[@]}" -ne 0 ]; then
    printf 'error: %d payload(s) failed: %s\n' "${#failed[@]}" "${failed[*]}" >&2
    printf 're-run scripts/vendor.sh to resume\n' >&2
    exit 1
fi

printf '==> all %d payload(s) present in %s\n' "${#selected[@]}" "vendor/blobs"
