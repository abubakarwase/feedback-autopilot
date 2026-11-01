#!/usr/bin/env bash
# Start the API (feedback intake + target app) on :8000 and the Streamlit UI on :8501.
# Logs go to logs/api.log and logs/ui.log. Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."

die() { echo "[start] ERROR: $*" >&2; exit 1; }

[ -f ui/streamlit_app.py ] || die "ui/streamlit_app.py not found"
for port in 8000 8501; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
    die "port $port is already in use: $(lsof -nP -iTCP:"$port" -sTCP:LISTEN | tail -n +2 | awk '{print $1, "pid", $2}' | head -1)"
  fi
done
mkdir -p logs

API_PID=""
UI_PID=""
stop() {
  trap - INT TERM EXIT
  echo "[start] stopping"
  for pid in $API_PID $UI_PID; do
    pkill -TERM -P "$pid" 2>/dev/null || true   # uv's child (uvicorn / streamlit)
    kill -TERM "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap 'stop; exit 130' INT TERM
trap stop EXIT

uv run uvicorn app.api:app --host 127.0.0.1 --port 8000 >logs/api.log 2>&1 &
API_PID=$!
uv run streamlit run ui/streamlit_app.py --server.port 8501 --server.headless true >logs/ui.log 2>&1 &
UI_PID=$!

echo "[start] api        http://localhost:8000/app/#/shipments   (log: logs/api.log)"
echo "[start] feedback   POST http://localhost:8000/feedback"
echo "[start] ui         http://localhost:8501                   (log: logs/ui.log)"
echo "[start] Ctrl-C stops both"

while kill -0 "$API_PID" 2>/dev/null && kill -0 "$UI_PID" 2>/dev/null; do
  sleep 1
done
kill -0 "$API_PID" 2>/dev/null || die "api exited, see logs/api.log"
die "streamlit exited, see logs/ui.log"
