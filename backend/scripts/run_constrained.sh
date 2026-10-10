#!/usr/bin/env bash
# Run the backend inside a transient cgroup with injected OS resource limits.
#
#   MEMORY_MAX=1G CPU_QUOTA=100% OS_THREADS=1 PORT=8100 bash scripts/run_constrained.sh
#
# MEMORY_MAX is a hard cap: the kernel OOM-kills the process beyond it.
# MEMORY_HIGH (default 90% of MEMORY_MAX) throttles and reclaims before that.
# Swap is disabled for the scope so measurements reflect real RAM.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MEMORY_MAX="${MEMORY_MAX:-1G}"
CPU_QUOTA="${CPU_QUOTA:-100%}"
TASKS_MAX="${TASKS_MAX:-256}"
PORT="${PORT:-8100}"
WORKERS="${WORKERS:-1}"
PYTHON="${PYTHON:-$APP_DIR/venv/bin/python}"

to_bytes() {
    local value="${1^^}"
    case "$value" in
        *K) echo $(( ${value%K} * 1024 )) ;;
        *M) echo $(( ${value%M} * 1024 ** 2 )) ;;
        *G) echo $(( ${value%G} * 1024 ** 3 )) ;;
        *[0-9]) echo "$value" ;;
        *) echo "Unsupported size: $1" >&2; exit 1 ;;
    esac
}
MEMORY_HIGH="${MEMORY_HIGH:-$(( $(to_bytes "$MEMORY_MAX") * 9 / 10 ))}"

if ! [[ "$WORKERS" =~ ^[1-9][0-9]*$ && "$PORT" =~ ^[0-9]+$ ]]; then
    echo "WORKERS and PORT must be positive integers" >&2
    exit 1
fi

# OS_* variables are read by app/os_limits.py inside each worker.
export OS_THREADS="${OS_THREADS:-1}"
export OS_MIN_AVAILABLE_MB="${OS_MIN_AVAILABLE_MB:-64}"
export OS_MODEL_LOAD_MB="${OS_MODEL_LOAD_MB:-300}"
[[ -n "${OS_CPU_AFFINITY:-}" ]] && export OS_CPU_AFFINITY
[[ -n "${OS_NICE:-}" ]] && export OS_NICE

echo "cgroup: MemoryMax=$MEMORY_MAX MemoryHigh=$MEMORY_HIGH CPUQuota=$CPU_QUOTA TasksMax=$TASKS_MAX; app: OS_THREADS=$OS_THREADS OS_MIN_AVAILABLE_MB=$OS_MIN_AVAILABLE_MB OS_MODEL_LOAD_MB=$OS_MODEL_LOAD_MB workers=$WORKERS port=$PORT" >&2
cd "$APP_DIR"
exec systemd-run --user --scope --quiet --collect \
    --unit="teamsynapse-constrained-$PORT-$$" \
    -p MemoryMax="$MEMORY_MAX" -p MemoryHigh="$MEMORY_HIGH" -p MemorySwapMax=0 \
    -p CPUQuota="$CPU_QUOTA" -p TasksMax="$TASKS_MAX" \
    "$PYTHON" -m gunicorn -w "$WORKERS" -k uvicorn.workers.UvicornWorker app.main:app \
    --bind "127.0.0.1:$PORT" --timeout 120
