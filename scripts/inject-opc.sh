#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

TOOL="$ROOT/injections/chaos-opcua"
CLI="$TOOL/.venv/bin/chaos-opcua"
BASE_CONFIG="$TOOL/configs/lab-server-stop.yaml"

[ -x "$CLI" ] || fail "chaos-opcua is not set up in $TOOL"
[ -f "$BASE_CONFIG" ] || fail "missing $BASE_CONFIG"

print_report() {
  "$PY" - "$1" <<'PY'
import json
import sys

with open(sys.argv[1]) as handle:
    r = json.load(handle)
c = r["metrics"]["calculated"]
o = r["outcomes"]

def show(value, unit="s"):
    return "n/a" if value is None else f"{value}{unit}"

gaps = [t["gap_s"] for t in (c.get("telemetry_gap") or {}).get("per_tag", {}).values() if t.get("gap_s") is not None]
interval = (c.get("baseline_telemetry_update_interval") or {}).get("median_of_tag_medians_s")
rows = [
    ("baseline telemetry interval", show(interval)),
    ("fault duration", show(c.get("actual_fault_duration_s"))),
    ("probe saw server down", show(c.get("time_to_probe_failure_s"))),
    ("MES reported disconnect", show(c.get("time_to_mes_reported_disconnect_s"))),
    ("MES detection delay", show(c.get("mes_detection_delay_s"))),
    ("telemetry went stale", show(c.get("time_to_telemetry_stale_s"))),
    ("endpoint reachable after restore", show(c.get("time_to_endpoint_reachable_s"))),
    ("MES reconnected after restore", show(c.get("time_to_mes_reconnected_s"))),
    ("first fresh telemetry", show(c.get("time_to_first_fresh_telemetry_s"))),
    ("all tags fresh", show(c.get("time_to_all_tags_fresh_s"))),
    ("longest telemetry gap", show(max(gaps) if gaps else None)),
]

print()
print(f"run      {r['experiment_id']} / {r['run_id']}")
print(f"status   {r['overall_status']}")
print(f"outcome  injection={o['injection']}  impact={o['impact']}  recovery={o['recovery']}  cleanup={o['cleanup']}")
print(f"evidence {(r.get('evidence') or {}).get('level')}")
print()
width = max(len(name) for name, _ in rows)
for name, value in rows:
    print(f"{name:<{width}}  {value:>9}")
impact = (r["metrics"].get("inferred_impact") or {}).get("production_during_fault") or {}
resets = [tag for tag, v in impact.items() if v.get("counter_reset")]
if resets:
    print()
    print(f"counter reset  {', '.join(resets)}")
if r.get("missing_metrics"):
    print()
    for item in r["missing_metrics"]:
        print(f"missing  {item}")
if r.get("error"):
    print()
    print(f"error    {r['error']}")
print()
print(f"report   {sys.argv[1]}")
PY
}

latest_report() {
  ls -t "$TOOL"/results/*.json 2>/dev/null | head -n 1
}

if [ "${1:-}" = "last" ]; then
  report="$(latest_report)"
  [ -n "$report" ] || fail "no reports yet"
  print_report "$report"
  exit 0
fi

FAULT="${1:-20}"
BASELINE="${2:-30}"
[[ "$FAULT" =~ ^[0-9]+$ ]] && [ "$FAULT" -ge 1 ] && [ "$FAULT" -le 60 ] || fail "fault seconds must be 1-60"
[[ "$BASELINE" =~ ^[0-9]+$ ]] && [ "$BASELINE" -ge 10 ] && [ "$BASELINE" -le 120 ] || fail "baseline seconds must be 10-120"

config="$TOOL/configs/.inject-$$.yaml"
trap 'rm -f "$config"' EXIT
sed -e "s/^  fault_duration_s: .*/  fault_duration_s: $FAULT/" \
    -e "s/^  baseline_duration_s: .*/  baseline_duration_s: $BASELINE/" \
    "$BASE_CONFIG" > "$config"

cd "$TOOL"

running="$("$CLI" mes status --config "$config" 2>/dev/null | grep -c ' running ' || true)"
if [ "$running" -lt 3 ]; then
  echo "starting the MES stack"
  "$CLI" mes start --config "$config" > /dev/null || fail "mes start failed; see $TOOL/run/logs"
  sleep 20
fi

echo "preflight"
if ! "$CLI" preflight --config "$config" > /dev/null 2>&1; then
  sleep 15
  "$CLI" preflight --config "$config" | grep -E "FAIL|PASSED|FAILED" || true
  "$CLI" preflight --config "$config" > /dev/null 2>&1 || fail "preflight failed; nothing was injected"
fi

echo "injecting: stop the OPC UA server for ${FAULT}s after a ${BASELINE}s baseline"
before="$(latest_report)"
status=0
"$CLI" run --config "$config" --target fsmes-opc-sim > "$TOOL/run/inject-last.log" 2>&1 || status=$?
report="$(latest_report)"

if [ -z "$report" ] || [ "$report" = "$before" ]; then
  tail -n 20 "$TOOL/run/inject-last.log"
  fail "no report was written (exit $status)"
fi
print_report "$report"
exit "$status"
