#!/usr/bin/env bash
# Stop the lab's container stack. Keeps the database volume unless --volumes is given.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$ROOT"
args=(stop)
[ "${1:-}" = "--remove" ] && args=(down)
[ "${1:-}" = "--volumes" ] && args=(down --volumes)
docker compose --env-file "$ROOT/runtime/state/docker.env" -f docker/compose.lab.yml "${args[@]}"
