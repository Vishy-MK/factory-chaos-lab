#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

DURATION="${1:-30}"
BASELINE="${2:-20}"
WORKLOAD="${3:-baseline}"

[[ "$DURATION" =~ ^[0-9]+$ ]] || fail "duration must be an integer (1-60)"
[[ "$BASELINE" =~ ^[0-9]+$ ]] || fail "baseline must be an integer (5-60)"

mkdir -p "$ROOT/runtime/state"
definition="$ROOT/runtime/state/baseline-$$.yaml"
output="$ROOT/runtime/state/baseline-$$.json"
trap 'rm -f "$definition" "$output"' EXIT

cat > "$definition" <<EOF
name: baseline-observe
description: Baseline observation, ${DURATION}s, no fault.
category: infrastructure
target:
  plant: bottling
fault:
  type: none
  parameters:
    duration_s: ${DURATION}
baseline_s: ${BASELINE}
workload: ${WORKLOAD}
recovery:
  timeout_s: 30
EOF

echo "baseline: observe ${DURATION}s after a ${BASELINE}s warm-up, workload ${WORKLOAD}"
status=0
chaoslab --json run "$definition" > "$output" || status=$?

"$PY" - "$output" <<'PY'
import json
import sys

with open(sys.argv[1]) as handle:
    text = handle.read()
try:
    r = json.loads(text)
except json.JSONDecodeError:
    print(text)
    sys.exit(1)

def show(value, unit=""):
    return "n/a" if value is None else f"{value}{unit}"

print()
print(f"id      {r['experiment_id']}")
print(f"result  {r['overall_result']}")
for reason in r.get("reasons") or []:
    print(f"        {reason}")
metrics = r.get("observed_metrics") or {}
rows = [("baseline", "baseline"), ("fault", "observed"), ("post", "post")]
header = f"{'phase':<10}{'n':>4}{'api ok':>8}{'p50 ms':>9}{'p95 ms':>9}{'max ms':>9}{'conn':>6}{'tags/s':>9}{'floor s':>9}"
print()
print(header)
print("-" * len(header))
for key, label in rows:
    m = metrics.get(key) or {}
    if not m.get("samples"):
        continue
    latency = m.get("api_latency_ms") or {}
    print(f"{label:<10}{m['samples']:>4}{show(m.get('api_success_ratio')):>8}{show(latency.get('p50')):>9}"
          f"{show(latency.get('p95')):>9}{show(latency.get('max')):>9}{show(m.get('machines_connected_min')):>6}"
          f"{show(m.get('tag_ingest_per_second')):>9}{show(m.get('floor_seconds_since_check_max')):>9}")
observed = metrics.get("fault") or {}
print()
print(f"rss MB  {observed.get('component_rss_mb_max')}")
print(f"host    min available {show(observed.get('host_available_memory_mb_min'), ' MB')}, "
      f"max cpu {show(observed.get('host_cpu_percent_max'), '%')}")
workload = r.get("workload") or {}
print(f"load    {workload.get('total_requests', 0)} requests, {workload.get('total_errors', 0)} errors")
print(f"file    results/{r['experiment_id']}/result.json")
PY
exit "$status"
