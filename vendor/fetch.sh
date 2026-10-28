#!/usr/bin/env bash
# Source from a Docker build step after mounting vendor/ at /vendor.
verified_payload() {
    local name=$1 target=$2 record want url arch extra vendor_root=${VENDOR_ROOT:-/vendor}
    record=$(awk -v n="$name" '!/^#/ && NF==4 && $2==n {print; count++} END {if (count != 1) exit 1}' "$vendor_root/manifest.txt") \
        || { echo "error: missing or duplicate manifest entry: $name" >&2; return 1; }
    read -r want _ url arch extra <<< "$record"
    [[ "$want" =~ ^[0-9a-f]{64}$ && "$url" == https://* && -z "$extra" &&
       ( "$arch" == any || "$arch" == amd64 || "$arch" == arm64 ) ]] \
        || { echo "error: invalid manifest entry: $name" >&2; return 1; }
    if [ -f "$vendor_root/blobs/$name" ] && [ "$(sha256sum "$vendor_root/blobs/$name" | cut -d' ' -f1)" = "$want" ]; then
        cp "$vendor_root/blobs/$name" "$target"
    else
        echo "fetching manifest payload: $name"
        curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 \
            --connect-timeout 20 --max-time 900 -o "$target" "$url" || return 1
    fi
    if [ "$(sha256sum "$target" | cut -d' ' -f1)" != "$want" ]; then
        rm -f "$target"
        echo "error: checksum mismatch: $name" >&2
        return 1
    fi
}
