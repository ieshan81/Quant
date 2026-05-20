#!/bin/bash
cd "$(dirname "$0")"
set -e

PORT="${PORT:-5000}"

# Dashboard must bind /health before the worker imports heavy trading stacks.
python monitoring/dashboard.py &
DASH_PID=$!
echo "Dashboard starting (pid ${DASH_PID}) on port ${PORT}"

_ready=0
for _ in $(seq 1 120); do
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT}/health', timeout=2)" 2>/dev/null; then
    _ready=1
    break
  fi
  sleep 1
done
echo "Dashboard health ready=${_ready}"

python main_worker.py || echo "worker exited (non-fatal for dashboard)" &
echo "Worker started in background"

wait "${DASH_PID}"
