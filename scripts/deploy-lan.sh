#!/usr/bin/env bash
# Serve the whole lab to this PC's LAN: Grafana, the MES web app and API, the TeamSynapse RAG backend and the lab's
# control API, each behind its own login, through one gateway that only admits clients from the LAN subnet.
#
#   scripts/deploy-lan.sh up       start everything (plant, monitoring, RAG backend, control API, gateway)
#   scripts/deploy-lan.sh status   what is running and the URLs
#   scripts/deploy-lan.sh down     stop the LAN exposure (gateway, control API, RAG backend); --all also stops
#                                  monitoring and the plant
#
# Secrets are generated once into runtime/state/deployment.env (mode 600) and reused. Nothing here needs sudo; the
# firewall rules it prints are optional hardening for you to run.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_venv
STATE="$ROOT/runtime/state"; LOGS="$ROOT/runtime/logs"; ENVFILE="$STATE/deployment.env"
mkdir -p "$STATE" "$LOGS"
config() { "$PY" -c "import yaml,sys; d=yaml.safe_load(open('$ROOT/config/deployment.yaml')); print(eval(sys.argv[1]))" "$1"; }
RAG_DIR="$(config "d['rag_backend']['directory']")"; RAG_PORT="$(config "d['rag_backend']['port']")"

lan_ip() {
  "$PY" -c "
import yaml; from chaoslab.deploy import network
c = yaml.safe_load(open('$ROOT/config/deployment.yaml')); i, a, allowed = network.lan(c)
print(a.ip, a.network, i)"
}
running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
secret() { "$PY" -c "import secrets; print(secrets.token_urlsafe(24))"; }

ensure_secrets() {
  [ -f "$ENVFILE" ] && return 0
  umask 077
  {
    echo "# LAN deployment secrets, generated $(date -Iseconds). Keep private."
    echo "GRAFANA_ADMIN_PASSWORD=$(secret)"
    echo "GRAFANA_VIEWER_PASSWORD=$(secret)"
    echo "CHAOSLAB_CONTROL_TOKEN=$(secret)$(secret)"
    echo "BACKEND_API_KEY=$(secret)$(secret)"
    echo "FSMES_ADMIN_PASSWORD=$(secret)"
    echo "FSMES_SCOTT_PASSWORD=$(secret)"
    echo "FSMES_AGENT_PASSWORD=$(secret)"
    echo "FSMES_ANALYST_PASSWORD=$(secret)"
  } > "$ENVFILE"
  chmod 600 "$ENVFILE"
  echo "generated secrets in $ENVFILE"
}

load_secrets() { set -a; source "$ENVFILE"; set +a; }

start_bg() {  # start_bg NAME COMMAND...
  local name="$1"; shift
  running "$STATE/$name.pid" && { echo "$name already running"; return 0; }
  (cd "$ROOT" && exec nohup "$@" >> "$LOGS/$name.log" 2>&1 < /dev/null) &
  echo $! > "$STATE/$name.pid"
}

stop_bg() {
  local name="$1"
  if running "$STATE/$name.pid"; then
    kill -TERM -- "$(cat "$STATE/$name.pid")" 2>/dev/null || kill -TERM "$(cat "$STATE/$name.pid")"
    for _ in $(seq 20); do running "$STATE/$name.pid" || break; sleep 0.5; done
    running "$STATE/$name.pid" && kill -KILL "$(cat "$STATE/$name.pid")"
    echo "stopped $name"
  fi
  rm -f "$STATE/$name.pid"
}

grafana_accounts() {
  local api="http://127.0.0.1:3000"
  for _ in $(seq 30); do curl -fsS -m 2 "$api/api/health" > /dev/null 2>&1 && break; sleep 1; done
  if ! curl -fsS -m 5 -u "admin:$GRAFANA_ADMIN_PASSWORD" "$api/api/user" > /dev/null 2>&1; then
    # First deployment: the admin still has the documented lab default.
    local old="${GRAFANA_PREVIOUS_ADMIN_PASSWORD:-lab-only-grafana}"
    curl -fsS -m 5 -u "admin:$old" -X PUT -H 'Content-Type: application/json' \
      -d "{\"password\":\"$GRAFANA_ADMIN_PASSWORD\"}" "$api/api/admin/users/1/password" > /dev/null \
      || fail "could not set the Grafana admin password (set GRAFANA_PREVIOUS_ADMIN_PASSWORD to the current one)"
    echo "grafana admin password replaced"
  fi
  local id
  id="$(curl -fsS -m 5 -u "admin:$GRAFANA_ADMIN_PASSWORD" "$api/api/users/lookup?loginOrEmail=viewer" 2>/dev/null \
        | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null || true)"
  if [ -z "$id" ]; then
    curl -fsS -m 5 -u "admin:$GRAFANA_ADMIN_PASSWORD" -X POST -H 'Content-Type: application/json' \
      -d "{\"name\":\"Viewer\",\"login\":\"viewer\",\"password\":\"$GRAFANA_VIEWER_PASSWORD\"}" \
      "$api/api/admin/users" > /dev/null && echo "grafana viewer account created"
  else
    curl -fsS -m 5 -u "admin:$GRAFANA_ADMIN_PASSWORD" -X PUT -H 'Content-Type: application/json' \
      -d "{\"password\":\"$GRAFANA_VIEWER_PASSWORD\"}" "$api/api/admin/users/$id/password" > /dev/null
    curl -fsS -m 5 -u "admin:$GRAFANA_ADMIN_PASSWORD" -X PATCH -H 'Content-Type: application/json' \
      -d '{"role":"Viewer"}' "$api/api/orgs/1/users/$id" > /dev/null || true
  fi
}

urls() {
  read -r ip net iface <<< "$(lan_ip)"
  cat <<TXT

Open from any PC on $net (via $iface):
  Grafana dashboards   http://$ip:3000            viewer / GRAFANA_VIEWER_PASSWORD   (admin / GRAFANA_ADMIN_PASSWORD)
  MES web app and API  http://$ip:8010            MES accounts: ADMIN / FSMES_ADMIN_PASSWORD, SCOTT / FSMES_SCOTT_PASSWORD
  RAG backend          http://$ip:8200/docs       header X-API-Key: BACKEND_API_KEY
  Lab control API      http://$ip:8090/api/help   header Authorization: Bearer CHAOSLAB_CONTROL_TOKEN
Passwords and keys: $ENVFILE (this PC only; share them privately).
Not exposed: Prometheus (9090) and the exporter (9108) stay on 127.0.0.1.

Optional firewall hardening (needs sudo), allowing only this LAN:
  sudo ufw allow from $net to any port 3000,8010,8200,8090 proto tcp
  sudo ufw enable
TXT
}

status() {
  read -r ip net iface <<< "$(lan_ip)"
  for name in gateway control-api rag-backend; do
    if running "$STATE/$name.pid"; then echo "$name: running (pid $(cat "$STATE/$name.pid"))"; else echo "$name: stopped"; fi
  done
  chaoslab status 2>/dev/null | head -1
  curl -fsS -m 2 http://127.0.0.1:3000/api/health > /dev/null 2>&1 && echo "grafana: running" || echo "grafana: stopped"
  echo "listening on $ip:"
  ss -ltn 2>/dev/null | awk -v ip="$ip" '$4 ~ "^"ip":" {print "  "$4}'
}

case "${1:-}" in
  up)
    read -r ip net iface <<< "$(lan_ip)" || fail "no private LAN address found"
    ensure_secrets; load_secrets
    if ! chaoslab health > /dev/null 2>&1; then
      chaoslab start
      for _ in $(seq 60); do chaoslab health > /dev/null 2>&1 && break; sleep 2; done
    fi
    chaoslab deploy-secure-plant || fail "could not replace the MES lab passwords; not exposing the plant"
    "$ROOT/scripts/stop-monitoring.sh" > /dev/null 2>&1 || true
    GRAFANA_DOMAIN="$ip" "$ROOT/scripts/start-monitoring.sh" --with-grafana
    grafana_accounts
    [ -x "$RAG_DIR/venv/bin/python" ] || fail "RAG backend venv missing in $RAG_DIR"
    start_bg rag-backend env BACKEND_API_KEY="$BACKEND_API_KEY" PORT="$RAG_PORT" \
      MEMORY_MAX="$(config "d['rag_backend']['memory_max']")" CPU_QUOTA="$(config "d['rag_backend']['cpu_quota']")" \
      OS_THREADS="$(config "d['rag_backend']['threads']")" bash "$RAG_DIR/scripts/run_constrained.sh"
    start_bg control-api env CHAOSLAB_CONTROL_TOKEN="$CHAOSLAB_CONTROL_TOKEN" "$PY" -m chaoslab --root "$ROOT" control-api
    start_bg gateway "$PY" -m chaoslab --root "$ROOT" gateway
    for _ in $(seq 30); do
      curl -fsS -m 2 "http://127.0.0.1:$RAG_PORT/health" > /dev/null 2>&1 && \
      curl -fsS -m 2 "http://$ip:8090/api/help" > /dev/null 2>&1 && break
      sleep 1
    done
    status
    urls
    ;;
  down)
    stop_bg gateway; stop_bg control-api; stop_bg rag-backend
    if [ "${2:-}" = "--all" ]; then "$ROOT/scripts/stop-monitoring.sh"; chaoslab stop; fi
    ;;
  status) status; urls ;;
  *) fail "usage: scripts/deploy-lan.sh up | status | down [--all]" ;;
esac
