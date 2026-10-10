#!/usr/bin/env bash
# Delay every plant database statement, then verify recovery; see docs/DATABASE_LATENCY.md.
#
#   scripts/inject-db-latency.sh [delay_ms] [duration_s] [baseline_s] [--agent] [--yes]
#   scripts/inject-db-latency.sh last
#
# Defaults: 200 ms for 10 s after a 20 s baseline. --agent has the Injector agent run it through the MCP tools
# (it asks you before the real fault unless --yes) and the Recovery Analyst judge it; it needs an LLM key.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/inject-common.sh"

if [ "${1:-}" = "last" ]; then
  id="$(ls -t "$ROOT/results" | grep -- '-db-latency-\|database-latency' | head -n 1)"
  [ -n "$id" ] || fail "no database latency results yet"
  chaoslab report "$id"
  exit 0
fi

mode=direct approve=no positional=()
for arg in "$@"; do
  case "$arg" in
    --agent) mode=agent ;;
    --yes) approve=yes ;;
    -*) fail "unknown option $arg" ;;
    *) positional+=("$arg") ;;
  esac
done
DELAY="${positional[0]:-200}"
DURATION="${positional[1]:-10}"
BASELINE="${positional[2]:-20}"
in_range delay_ms "$DELAY" 1 1000
in_range duration_s "$DURATION" 1 30
in_range baseline_s "$BASELINE" 5 60

echo "database latency injection"
echo "  target       plant=bottling resource=database (statements from api and opc-agent)"
echo "  delay_ms     $DELAY"
echo "  duration_s   $DURATION"
echo "  baseline_s   $BASELINE"
echo "  mode         $mode$([ "$mode" = agent ] && echo " (approval: $([ "$approve" = yes ] && echo given in advance || echo asked at the terminal))")"
echo

require_healthy_plant
[ "$mode" = agent ] && require_llm_key
name="db-latency-${DELAY}ms-${DURATION}s"
file="$(write_definition "$name" <<YAML
name: $name
description: Add $DELAY ms before every plant database statement for $DURATION seconds, then verify recovery.
category: infrastructure
target:
  plant: bottling
  resource: database
fault:
  type: database_latency
  parameters:
    delay_ms: $DELAY
    duration_s: $DURATION
baseline_s: $BASELINE
workload: baseline
recovery:
  timeout_s: 90
scoring: none
YAML
)"
echo "running (about $((BASELINE + DURATION + 30)) s)" >&2
id="$(run_definition "$file" "$mode" "$approve" | tail -n 1)"
echo
chaoslab report "$id"
[ "$mode" = agent ] && analyse_with_agent "$id"
result="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["overall_result"])' "$ROOT/results/$id/result.json")"
[ "$result" = recovered ] || exit 1
