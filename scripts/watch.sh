#!/usr/bin/env bash
# Watch the lab at the OS level: the plant's MES components and every lab process, by name, in a tree.
# MES components show as <plant>/<component>; lab processes as cl:<what>, with live state in their titles.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_venv
command -v htop >/dev/null || fail "htop is not installed (sudo apt install htop)"
PLANT="$(cd "$ROOT" && "$PY" -c 'from chaoslab import settings; print(settings.load().plant_name)')"
# htop rewrites its config on exit, so it gets a fresh copy of the lab's each time and never touches yours.
mkdir -p "$ROOT/runtime/htop"
cp "$ROOT/observability/htop/htoprc" "$ROOT/runtime/htop/htoprc"
HTOPRC="$ROOT/runtime/htop/htoprc" exec htop --tree --filter="cl:|$PLANT/" "$@"
