#!/bin/sh
set -eu

python3 - <<'PY'
import os
from core import init_db_path

init_db_path(os.environ.get("DB_PATH", "/data/scale.db"))
PY

exec python3 -m gunicorn \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-2}" \
  --timeout 30 \
  app:app
