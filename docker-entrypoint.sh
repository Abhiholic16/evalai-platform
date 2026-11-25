#!/bin/sh
set -eu

data_dir="${EVALAI_DATA_DIR:-/var/data}"
mkdir -p "$data_dir" "${EVALAI_UPLOAD_DIR:-$data_dir/uploads}"
chown 10001:10001 "$data_dir" "${EVALAI_UPLOAD_DIR:-$data_dir/uploads}"
exec setpriv --reuid=10001 --regid=10001 --clear-groups \
  uvicorn app.main:app --app-dir /app/backend --host 0.0.0.0 --port "${PORT:-8000}"
