#!/usr/bin/env bash
# Cap one plant component's CPU and/or RAM through cgroup v2, then verify recovery.
#
#   scripts/inject-resource-limit.sh <component> [--cpu PCT] [--memory-high MB] [--memory-max MB]
#                                    [--duration S] [--baseline S] [--agent] [--yes]
#   scripts/inject-resource-limit.sh last
#
# component: api | opc-sim | opc-agent | floor. Default: --cpu 20 for 15 s after a 20 s baseline.
# --memory-high throttles and reclaims; --memory-max is a hard limit (the kernel OOM-kills above it and cleanup
# restarts the component). The first run restarts the component once into its own lab scope before the baseline.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/inject-common.sh"

if [ "${1:-}" = "last" ]; then
  id="$(ls -t "$ROOT/results" | grep -- '-limit-\|memory-high\|cpu-limit' | head -n 1)"
  [ -n "$id" ] || fail "no resource limit results yet"
  chaoslab report "$id"
  exit 0
fi

COMPONENT="${1:-}"
case "$COMPONENT" in
  api|opc-sim|opc-agent|floor) shift ;;
  *) fail "first argument must be a component: api, opc-sim, opc-agent or floor" ;;
esac
CPU="" MEM_HIGH="" MEM_MAX="" DURATION=15 BASELINE=20 mode=direct approve=no
while [ $# -gt 0 ]; do
  case "$1" in
    --cpu) CPU="${2:-}"; shift 2 ;;
    --memory-high) MEM_HIGH="${2:-}"; shift 2 ;;
    --memory-max) MEM_MAX="${2:-}"; shift 2 ;;
    --duration) DURATION="${2:-}"; shift 2 ;;
    --baseline) BASELINE="${2:-}"; shift 2 ;;
    --agent) mode=agent; shift ;;
    --yes) approve=yes; shift ;;
    *) fail "unknown argument $1" ;;
  esac
done
[ -z "$CPU$MEM_HIGH$MEM_MAX" ] && CPU=20
[ -n "$CPU" ] && in_range cpu_percent "$CPU" 5 400
[ -n "$MEM_HIGH" ] && in_range memory_high_mb "$MEM_HIGH" 64 4096
[ -n "$MEM_MAX" ] && in_range memory_max_mb "$MEM_MAX" 64 4096
in_range duration_s "$DURATION" 1 30
in_range baseline_s "$BASELINE" 5 60

echo "resource limit injection (cgroup v2)"
echo "  target          plant=bottling component=$COMPONENT (scope chaoslab-bottling-$COMPONENT.scope)"
echo "  cpu_percent     ${CPU:-unchanged}"
echo "  memory_high_mb  ${MEM_HIGH:-unchanged}"
echo "  memory_max_mb   ${MEM_MAX:-unchanged}"
echo "  duration_s      $DURATION"
echo "  baseline_s      $BASELINE"
echo "  mode            $mode$([ "$mode" = agent ] && echo " (approval: $([ "$approve" = yes ] && echo given in advance || echo asked at the terminal))")"
echo

require_healthy_plant
[ "$mode" = agent ] && require_llm_key
label="${CPU:+cpu${CPU}}${MEM_HIGH:+-high${MEM_HIGH}}${MEM_MAX:+-max${MEM_MAX}}"
name="$COMPONENT-limit-${label#-}-${DURATION}s"
params="    duration_s: $DURATION"
[ -n "$CPU" ] && params+=$'\n'"    cpu_percent: $CPU"
[ -n "$MEM_HIGH" ] && params+=$'\n'"    memory_high_mb: $MEM_HIGH"
[ -n "$MEM_MAX" ] && params+=$'\n'"    memory_max_mb: $MEM_MAX"
file="$(write_definition "$name" <<YAML
name: $name
description: Cap $COMPONENT through its cgroup (cpu ${CPU:-unchanged}%, memory.high ${MEM_HIGH:-unchanged} MiB, memory.max ${MEM_MAX:-unchanged} MiB) for $DURATION seconds, then lift the limits.
category: infrastructure
target:
  plant: bottling
  component: $COMPONENT
fault:
  type: resource_limit
  parameters:
$params
baseline_s: $BASELINE
workload: baseline
recovery:
  timeout_s: 90
scoring: none
YAML
)"
echo "running (about $((BASELINE + DURATION + 30)) s, plus a one-time restart into the lab scope)" >&2
id="$(run_definition "$file" "$mode" "$approve" | tail -n 1)"
echo
chaoslab report "$id"
[ "$mode" = agent ] && analyse_with_agent "$id"
result="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["overall_result"])' "$ROOT/results/$id/result.json")"
[ "$result" = recovered ] || exit 1
