#!/bin/bash
# Railway / local: dashboard in background, worker in foreground (worker must stay up to trade).
set -e
cd "$(dirname "$0")"

PORT="${PORT:-5000}"
export PORT

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
if [ "${_ready}" != "1" ]; then
  echo "WARNING: dashboard /health not ready after 120s — worker will still start"
fi
echo "Dashboard health ready=${_ready}"

echo "Starting trading worker (foreground — container stays up while worker runs)"
exec python main_worker.py
