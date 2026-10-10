# Shared by scripts/inject-db-latency.sh and scripts/inject-resource-limit.sh; source after common.sh.

INJECT_DIR="$ROOT/runtime/state/inject"

# Usage: in_range NAME VALUE MIN MAX
in_range() {
  [[ "$2" =~ ^[0-9]+$ ]] && [ "$2" -ge "$3" ] && [ "$2" -le "$4" ] || fail "$1 must be an integer in $3..$4 (got '$2')"
}

require_healthy_plant() {
  chaoslab health > /dev/null 2>&1 || fail "the plant is not healthy; start it with scripts/start.sh and wait for HEALTHY"
}

require_llm_key() {
  local provider="${CHAOSLAB_LLM_PROVIDER:-mistral}"
  local key_env="MISTRAL_API_KEY"
  [ "$provider" = "groq" ] && key_env="GROQ_API_KEY"
  [ -n "${!key_env:-}" ] || fail "--agent needs an LLM key: export $key_env=... (provider $provider; set CHAOSLAB_LLM_PROVIDER to change it)"
}

# Usage: write_definition NAME  (definition YAML on stdin); prints the file path.
write_definition() {
  mkdir -p "$INJECT_DIR"
  local file="$INJECT_DIR/$1.yaml"
  cat > "$file"
  echo "$file"
}

# Usage: run_definition FILE MODE APPROVE  (MODE: direct | agent); prints the experiment id on the last line.
run_definition() {
  local file="$1" mode="$2" approve="$3"
  if [ "$mode" = "direct" ]; then
    local out status=0
    out="$(chaoslab --json run "$file")" || status=$?
    "$PY" -c 'import json,sys; print(json.loads(sys.stdin.read())["experiment_id"])' <<< "$out" \
      || fail "the run produced no result (exit $status)"
    return 0
  fi
  local name task out
  name="$(basename "$file" .yaml)"
  task="$("$PY" - "$file" <<'PY'
import json, sys, yaml
definition = yaml.safe_load(open(sys.argv[1]))
print("Run exactly this experiment definition, unchanged (pass it as the definition argument, not by name): "
      + json.dumps(definition, separators=(",", ":")))
PY
)"
  echo "injector agent: validating, dry-running, then asking before the real fault" >&2
  local args=(agent inject "$name" --task "$task")
  [ "$approve" = "yes" ] && args+=(--approve)
  out="$(chaoslab "${args[@]}")" || true
  local id
  id="$("$PY" -c 'import json,sys; d=json.loads(sys.stdin.read()); o=d.get("output") or {}; print(o.get("experiment_id") or "")' <<< "$out" 2>/dev/null || true)"
  if [ -z "$id" ]; then
    echo "$out" >&2
    fail "the injector did not run the experiment (see its output above)"
  fi
  echo "injector agent outcome: $("$PY" -c 'import json,sys; o=json.loads(sys.stdin.read())["output"]; print(o.get("outcome"), o.get("overall_result"), "emergency_stop_called=" + str(o.get("emergency_stop_called")))' <<< "$out")" >&2
  echo "$id"
}

# Usage: analyse_with_agent EXPERIMENT_ID
analyse_with_agent() {
  echo "recovery analyst agent: judging $1" >&2
  local out
  out="$(chaoslab agent analyze "$1")" || true
  "$PY" -c '
import json, sys
text = sys.stdin.read().strip()
payload = json.loads(text) if text else {}
o = payload.get("output") or {}
print()
print("analyst verdict   ", o.get("verdict"), "| agrees with lab:", o.get("agrees_with_lab"),
      "| time to recover:", o.get("time_to_recover_s"))
for item in (o.get("findings") or [])[:6]:
    print("  finding:", item)
for item in (o.get("evidence") or [])[:6]:
    print("  evidence:", item.get("claim"), "[" + str(item.get("source")) + "]")
if payload.get("analysis_file"):
    print("analysis file     ", payload["analysis_file"])
' <<< "$out"
}
