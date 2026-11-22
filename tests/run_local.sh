#!/usr/bin/env bash
# Local end-to-end driver: start the service, run the pre-restart tests,
# bounce the process, run the post-restart tests.
set -euo pipefail

PY=${PY:-python3}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DATA=${DATA_DIR:-/tmp/zipfetch-data}
PORT=${PORT:-8123}
BASE="http://127.0.0.1:${PORT}"

rm -rf "$DATA"
mkdir -p "$DATA/sources"
export DATA_DIR="$DATA"

cd "$ROOT"

start_server() {
    "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" &
    SRV=$!
    for _ in $(seq 1 100); do
        curl -sf "$BASE/healthz" >/dev/null 2>&1 && return 0
        sleep 0.2
    done
    echo "server did not come up" >&2
    return 1
}

stop_server() {
    kill "$SRV" 2>/dev/null || true
    wait "$SRV" 2>/dev/null || true
}

trap stop_server EXIT

echo "--- starting server (data: $DATA, port: $PORT)"
start_server

echo "--- phase: pre-restart"
"$PY" -I "$ROOT/tests/selftest.py" --base "$BASE" --sources "$DATA/sources" \
    --state "$DATA/state.json" --phase pre-restart

echo "--- restarting server"
stop_server
start_server

echo "--- phase: post-restart"
"$PY" -I "$ROOT/tests/selftest.py" --base "$BASE" --sources "$DATA/sources" \
    --state "$DATA/state.json" --phase post-restart

stop_server
trap - EXIT
echo "=== ALL SELF-TESTS PASSED ==="
