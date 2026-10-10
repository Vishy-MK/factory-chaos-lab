#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

BASE="$ROOT/runtime/monitoring"
STATE="$ROOT/runtime/state"
PROMETHEUS_DIR="$(find "$BASE" -maxdepth 1 -type d -name 'prometheus-[0-9]*' 2>/dev/null | sort | tail -n 1)"
[ -n "$PROMETHEUS_DIR" ] && [ -x "$PROMETHEUS_DIR/prometheus" ] || fail "prometheus is not installed; run scripts/install-monitoring.sh"
mkdir -p "$STATE" "$ROOT/runtime/logs" "$BASE/prometheus-data"

WITH_GRAFANA=0
for arg in "$@"; do
  case "$arg" in
    --with-grafana) WITH_GRAFANA=1 ;;
    *) fail "unknown argument: $arg (supported: --with-grafana)" ;;
  esac
done

available_mb="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)"
threshold_mb="${MONITORING_MIN_AVAILABLE_MB:-1800}"
if [ "$available_mb" -lt "$threshold_mb" ]; then
  fail "only ${available_mb} MB available (threshold ${threshold_mb} MB); not starting monitoring"
fi

running() {
  local pidfile="$1"
  [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

launch() {
  local name="$1" log="$2" cwd="$3"
  shift 3
  (cd "$cwd" && exec nohup "$@" >> "$log" 2>&1 < /dev/null) &
  echo $! > "$STATE/$name.pid"
}

chaoslab status >/dev/null 2>&1 || echo "note: the lab plant is not healthy; metrics will show it as down"
if ! running "$STATE/exporter.pid"; then
  launch exporter "$ROOT/runtime/logs/exporter.log" "$ROOT" "$PY" -m chaoslab --root "$ROOT" exporter
  sleep 1
fi
curl -fsS -m 5 http://127.0.0.1:9108/healthz >/dev/null || fail "the exporter did not start; see runtime/logs/exporter.log"

if ! running "$STATE/prometheus.pid"; then
  launch prometheus "$ROOT/runtime/logs/prometheus.log" "$ROOT" "$PROMETHEUS_DIR/prometheus" \
    --config.file="$ROOT/observability/prometheus/prometheus.yml" \
    --storage.tsdb.path="$BASE/prometheus-data" \
    --storage.tsdb.retention.time=3d \
    --storage.tsdb.retention.size=512MB \
    --web.listen-address=127.0.0.1:9090
fi
for _ in $(seq 1 30); do
  curl -fsS -m 2 http://127.0.0.1:9090/-/ready >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS -m 2 http://127.0.0.1:9090/-/ready >/dev/null || fail "prometheus did not become ready; see runtime/logs/prometheus.log"
echo "prometheus ready on http://127.0.0.1:9090 (exporter on http://127.0.0.1:9108/metrics)"

if [ "$WITH_GRAFANA" -eq 1 ]; then
  GRAFANA_DIR="$(find "$BASE" -maxdepth 1 -type d -name 'grafana-v*' 2>/dev/null | sort | tail -n 1)"
  [ -n "$GRAFANA_DIR" ] || fail "grafana is not installed; run scripts/install-monitoring.sh --with-grafana"
  available_mb="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)"
  [ "$available_mb" -ge 1500 ] || fail "only ${available_mb} MB available; not starting grafana"
  mkdir -p "$BASE/grafana-data" "$BASE/grafana-provisioning/datasources" "$BASE/grafana-provisioning/dashboards"
  cp "$ROOT/observability/grafana/provisioning/datasources/"*.yaml "$BASE/grafana-provisioning/datasources/"
  sed "s#__DASHBOARD_DIR__#$ROOT/observability/grafana/dashboards#" \
    "$ROOT/observability/grafana/provisioning/dashboards/dashboards.yaml" \
    > "$BASE/grafana-provisioning/dashboards/dashboards.yaml"
  if ! running "$STATE/grafana.pid"; then
    GF_PATHS_DATA="$BASE/grafana-data" GF_PATHS_LOGS="$ROOT/runtime/logs" \
    GF_PATHS_PROVISIONING="$BASE/grafana-provisioning" GF_SERVER_HTTP_ADDR=127.0.0.1 GF_SERVER_HTTP_PORT=3000 \
    GF_ANALYTICS_REPORTING_ENABLED=false GF_ANALYTICS_CHECK_FOR_UPDATES=false \
    GF_SECURITY_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-lab-only-grafana}" \
    launch grafana "$ROOT/runtime/logs/grafana.log" "$GRAFANA_DIR" ./bin/grafana server --homepath "$GRAFANA_DIR"
  fi
  for _ in $(seq 1 60); do
    curl -fsS -m 2 http://127.0.0.1:3000/api/health >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fsS -m 2 http://127.0.0.1:3000/api/health >/dev/null || fail "grafana did not become ready; see runtime/logs/grafana.log"
  echo "grafana ready on http://127.0.0.1:3000"
fi
