#!/usr/bin/env bash
# Launch the local Job Monitor. Binds to loopback only.
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${JOBMON_HOST:-127.0.0.1}"
PORT="${JOBMON_PORT:-8765}"
DATA_DIR="${JOBMON_DATA_DIR:-data}"

if [ -f "${DATA_DIR}/db.sqlite3" ]; then
  echo "Backing up before migration…"
  uv run python manage.py backup --keep 7
fi

uv run python manage.py migrate --noinput
echo "Job Monitor: http://${HOST}:${PORT}/"
exec uv run python manage.py runserver "${HOST}:${PORT}" --insecure --noreload
