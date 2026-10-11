#!/usr/bin/env bash
# Start the lab's container stack (docker/compose.lab.yml): PostgreSQL, migrate, API, OPC sim, OPC agent.
# The lightweight plant is not touched. Use the stack with CHAOSLAB_DEPLOYMENT=docker, e.g.
#   CHAOSLAB_DEPLOYMENT=docker python -m chaoslab run experiments/definitions/docker-api-stop-10s.yaml
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_venv
command -v docker > /dev/null || fail "docker is not installed"
docker ps > /dev/null 2>&1 || fail "your user cannot use Docker yet (docker group); see docs/DOCKER.md"
STATE="$ROOT/runtime/state"; ENVFILE="$STATE/docker.env"; DEPLOY="$STATE/deployment.env"
mkdir -p "$STATE"
secret() { "$PY" -c "import secrets; print(secrets.token_urlsafe(24))"; }
if [ ! -s "$ENVFILE" ]; then
  umask 077
  analyst="$(grep -s '^FSMES_ANALYST_PASSWORD=' "$DEPLOY" | cut -d= -f2-)"
  if [ -z "$analyst" ]; then
    analyst="$(secret)"
    echo "FSMES_ANALYST_PASSWORD=$analyst" >> "$DEPLOY"; chmod 600 "$DEPLOY"
  fi
  {
    echo "# Docker stack secrets, generated $(date -Iseconds). Keep private."
    echo "POSTGRES_PASSWORD=$(secret)"
    echo "MES_SECRET_KEY=$(secret)$(secret)"
    echo "MES_ADMIN_PASSWORD=$(secret)"
    echo "MES_OPERATOR_PASSWORD=$(secret)"
    echo "FSMES_ANALYST_PASSWORD=$analyst"
  } > "$ENVFILE"
  chmod 600 "$ENVFILE"
  echo "generated $ENVFILE"
fi
available_mb="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)"
[ "$available_mb" -ge 1500 ] || fail "only ${available_mb} MB available; the stack needs about 1 GB plus headroom"
cd "$ROOT"
docker compose --env-file "$ENVFILE" -f docker/compose.lab.yml up -d --build --wait --wait-timeout 240
CHAOSLAB_DEPLOYMENT=docker "$PY" -m chaoslab --root "$ROOT" status
