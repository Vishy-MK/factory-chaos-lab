#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
SERVICE=bosch-backend
# Four workers are retained for compatibility; use GUNICORN_WORKERS=1 for
# memory-constrained local embedding deployments. Index writes are locked.
GUNICORN_WORKERS="${GUNICORN_WORKERS:-4}"
if ! [[ "$GUNICORN_WORKERS" =~ ^[1-9][0-9]*$ ]]; then
    echo "GUNICORN_WORKERS must be a positive integer" >&2
    exit 1
fi
# Optional OS-level resource injection (see README "Low-memory machines").
# MEMORY_MAX/CPU_QUOTA become systemd cgroup limits for the whole service;
# OS_* variables are applied inside each worker by app/os_limits.py.
LIMIT_LINES=""
for setting in "MemoryMax=${MEMORY_MAX:-}" "MemoryHigh=${MEMORY_HIGH:-}" "CPUQuota=${CPU_QUOTA:-}"; do
    value="${setting#*=}"
    if [[ -n "$value" ]]; then
        if ! [[ "$value" =~ ^[0-9]+[KMGT%]?$ ]]; then
            echo "Invalid systemd limit: $setting" >&2
            exit 1
        fi
        LIMIT_LINES+="$setting"$'\n'
    fi
done
[[ -n "${MEMORY_MAX:-}" ]] && LIMIT_LINES+="MemorySwapMax=0"$'\n'
for name in OS_THREADS OS_CPU_AFFINITY OS_NICE OS_MIN_AVAILABLE_MB OS_MODEL_LOAD_MB; do
    value="${!name:-}"
    if [[ -n "$value" ]]; then
        if ! [[ "$value" =~ ^[0-9,-]+$ ]]; then
            echo "Invalid $name: $value" >&2
            exit 1
        fi
        LIMIT_LINES+="Environment=$name=$value"$'\n'
    fi
done

sudo apt-get update
sudo apt-get install -y python3-pip python3-venv

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
# Install CPU-only torch before resolving Sentence Transformers dependencies.
"$APP_DIR/venv/bin/pip" install torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=Bosch FastAPI Inference Backend
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
${LIMIT_LINES}ExecStart=$APP_DIR/venv/bin/gunicorn -w $GUNICORN_WORKERS -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:8000 --timeout 120
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable $SERVICE
sudo systemctl restart $SERVICE
sudo systemctl status $SERVICE --no-pager
