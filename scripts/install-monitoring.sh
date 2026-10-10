#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

PROMETHEUS_VERSION="${PROMETHEUS_VERSION:-3.15.0}"
GRAFANA_VERSION="${GRAFANA_VERSION:-13.2.3}"
WITH_GRAFANA=0
for arg in "$@"; do
  case "$arg" in
    --with-grafana) WITH_GRAFANA=1 ;;
    *) fail "unknown argument: $arg (supported: --with-grafana)" ;;
  esac
done

BASE="$ROOT/runtime/monitoring"
mkdir -p "$BASE"
command -v curl >/dev/null || fail "curl is required"
command -v sha256sum >/dev/null || fail "sha256sum is required"

install_prometheus() {
  local name="prometheus-${PROMETHEUS_VERSION}.linux-amd64"
  local target="$BASE/$name"
  if [ -x "$target/prometheus" ]; then
    echo "prometheus ${PROMETHEUS_VERSION} already installed"
    return
  fi
  local download="$BASE/downloads/$name"
  rm -rf "$download"
  mkdir -p "$download"
  local url="https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS_VERSION}"
  curl -fsSL -o "$download/$name.tar.gz" "$url/$name.tar.gz" || fail "download failed: $url/$name.tar.gz"
  curl -fsSL -o "$download/sha256sums.txt" "$url/sha256sums.txt" || fail "could not fetch checksums"
  (cd "$download" && grep " $name.tar.gz\$" sha256sums.txt | sha256sum -c -) || fail "checksum mismatch for $name"
  mkdir -p "$BASE/extract-$name"
  tar -xzf "$download/$name.tar.gz" -C "$BASE/extract-$name"
  mv "$BASE/extract-$name/$name" "$target"
  rmdir "$BASE/extract-$name"
  echo "installed $target"
}

install_grafana() {
  local name="grafana-${GRAFANA_VERSION}.linux-amd64"
  local target="$BASE/grafana-v${GRAFANA_VERSION}"
  if [ -x "$target/bin/grafana" ] || [ -x "$target/bin/grafana-server" ]; then
    echo "grafana ${GRAFANA_VERSION} already installed"
    return
  fi
  local download="$BASE/downloads/$name"
  rm -rf "$download"
  mkdir -p "$download"
  local url="https://dl.grafana.com/oss/release/$name.tar.gz"
  curl -fsSL -o "$download/$name.tar.gz" "$url" || fail "download failed: $url"
  curl -fsSL -o "$download/$name.tar.gz.sha256" "$url.sha256" || fail "could not fetch the grafana checksum"
  local expected actual
  expected="$(tr -d '[:space:]' < "$download/$name.tar.gz.sha256" | cut -c1-64)"
  actual="$(sha256sum "$download/$name.tar.gz" | cut -d' ' -f1)"
  [ "$expected" = "$actual" ] || fail "checksum mismatch for $name"
  mkdir -p "$BASE/extract-$name"
  tar -xzf "$download/$name.tar.gz" -C "$BASE/extract-$name"
  local extracted
  extracted="$(find "$BASE/extract-$name" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  mv "$extracted" "$target"
  rmdir "$BASE/extract-$name"
  echo "installed $target"
}

install_prometheus
if [ "$WITH_GRAFANA" -eq 1 ]; then
  install_grafana
fi
