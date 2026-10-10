#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
[ $# -ge 1 ] || fail "usage: scripts/run-experiment.sh experiments/definitions/<experiment>.yaml [--dry-run]"
file="$1"
shift
[ -f "$file" ] || [ -f "$ROOT/$file" ] || fail "experiment definition not found: $file"
[ -f "$file" ] || file="$ROOT/$file"
chaoslab run "$file" "$@"
