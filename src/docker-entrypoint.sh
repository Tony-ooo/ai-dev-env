#!/usr/bin/env bash
set -euo pipefail

if ! /usr/local/bin/s6-services-guard.sh; then
    printf '[docker-entrypoint] warning: s6 services guard failed\n' >&2
fi

exec /init "$@"
