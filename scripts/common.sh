set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

fail() {
  echo "error: $*" >&2
  exit 1
}

require_venv() {
  [ -x "$PY" ] || fail "$ROOT/.venv is missing; run scripts/setup.sh first"
}

chaoslab() {
  require_venv
  (cd "$ROOT" && "$PY" -m chaoslab --root "$ROOT" "$@")
}
