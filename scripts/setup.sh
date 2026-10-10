#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/factorysemantics/factorysemantics-mes.git}"
UPSTREAM_COMMIT="$(tr -d '[:space:]' < "$ROOT/UPSTREAM_COMMIT")"

echo "== prerequisites"
command -v python3 >/dev/null || fail "python3 not found; install Python 3.12 or newer"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  || fail "Python 3.12 or newer is required (found $(python3 --version 2>&1))"
python3 -c 'import venv, ensurepip' 2>/dev/null \
  || fail "python3-venv is missing; on Ubuntu run: sudo apt install python3-venv"
command -v git >/dev/null || fail "git not found; on Ubuntu run: sudo apt install git"

echo "== upstream checkout"
if [ ! -f "$ROOT/factorysemantics-mes/pyproject.toml" ]; then
  git clone "$UPSTREAM_URL" "$ROOT/factorysemantics-mes" || fail "could not clone $UPSTREAM_URL"
fi
current="$(git -C "$ROOT/factorysemantics-mes" rev-parse HEAD)"
if [ "$current" != "$UPSTREAM_COMMIT" ]; then
  if git -C "$ROOT/factorysemantics-mes" cat-file -e "$UPSTREAM_COMMIT" 2>/dev/null; then
    git -C "$ROOT/factorysemantics-mes" checkout --quiet "$UPSTREAM_COMMIT"
  else
    echo "warning: upstream is at $current, not the tested commit $UPSTREAM_COMMIT" >&2
  fi
fi
[ -f "$ROOT/factorysemantics-mes/LICENSE" ] || fail "upstream LICENSE is missing"
[ -f "$ROOT/factorysemantics-mes/NOTICE" ] || fail "upstream NOTICE is missing"

echo "== virtual environment"
if [ ! -x "$PY" ]; then
  python3 -m venv "$ROOT/.venv"
fi
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$ROOT/requirements-lab.txt"
"$PY" -m pip install --quiet -e "$ROOT/factorysemantics-mes"
"$ROOT/.venv/bin/fsmes" --version

echo "== lab runtime"
chaoslab prepare

echo "== checks"
chaoslab check
echo "setup complete; start the plant with scripts/start.sh"
