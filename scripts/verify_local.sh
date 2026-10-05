#!/usr/bin/env bash
# Local (non-Docker) equivalent of scripts/verify.sh.
#
# Runs unit tests, syntax/build checks, then starts a real uvicorn
# server, executes smoke stage 1, kills and restarts the server against
# the same database file, and executes smoke stage 2.
#
# Usage: scripts/verify_local.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
PORT="${API_PORT:-8080}"
BASE_URL="http://127.0.0.1:${PORT}"
WORKDIR="$(mktemp -d)"
DB="$WORKDIR/dosimetry.db"
STATE="$WORKDIR/state.json"
trap 'test -n "${SERVER_PID:-}" && kill "$SERVER_PID" >/dev/null 2>&1 || true; rm -rf "$WORKDIR"' EXIT

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "==> creating virtualenv"
  "$PYTHON" -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q -r requirements.txt
fi
PY="$ROOT/.venv/bin/python"

echo "==> [1/5] syntax / build check"
"$PY" -m py_compile app/*.py verify/smoke.py

echo "==> [2/5] unit tests"
"$PY" -m pytest -q tests

start_server() {
  DOSIMETRY_DB="$DB" "$PY" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$PORT" --log-level warning &
  SERVER_PID=$!
  for _ in $(seq 1 60); do
    if "$PY" -c "import urllib.request;urllib.request.urlopen('$BASE_URL/health',timeout=1)" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  echo "server failed to start" >&2
  return 1
}

stop_server() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
  wait "$SERVER_PID" 2>/dev/null || true
  SERVER_PID=""
}

echo "==> [3/5] start API + smoke stage 1"
start_server
BASE_URL="$BASE_URL" VERIFY_STATE="$STATE" STAGE=smoke1 "$PY" verify/smoke.py

echo "==> [4/5] restarting API process (same database file)"
stop_server
start_server

echo "==> [5/5] smoke stage 2 after restart"
BASE_URL="$BASE_URL" VERIFY_STATE="$STATE" STAGE=smoke2 "$PY" verify/smoke.py

echo
echo "ALL VERIFICATION PASSED: build check, unit tests, smoke1, restart, smoke2."
