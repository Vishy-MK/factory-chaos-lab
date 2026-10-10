#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
extra=()
for arg in "$@"; do
  case "$arg" in
    --with-exporter) extra+=("--with-exporter") ;;
    *) fail "unknown argument: $arg (supported: --with-exporter)" ;;
  esac
done
chaoslab start "${extra[@]}"
chaoslab status
