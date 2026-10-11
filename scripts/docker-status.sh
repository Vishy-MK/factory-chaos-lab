#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$ROOT"
docker compose --env-file "$ROOT/runtime/state/docker.env" -f docker/compose.lab.yml ps
CHAOSLAB_DEPLOYMENT=docker "$PY" -m chaoslab --root "$ROOT" status
