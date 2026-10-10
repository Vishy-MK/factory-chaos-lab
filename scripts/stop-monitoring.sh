#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
STATE="$ROOT/runtime/state"
for name in grafana prometheus exporter; do
  pidfile="$STATE/$name.pid"
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      echo "stopped $name ($pid)"
    fi
    rm -f "$pidfile"
  fi
done
