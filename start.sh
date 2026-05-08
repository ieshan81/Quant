#!/bin/bash
cd "$(dirname "$0")"
set -e
# Worker in background: failures must not stop the dashboard (Railway healthchecks /health).
python main_worker.py || echo "worker exited (non-fatal for dashboard)" &
WORKER_PID=$!
echo "Worker started with PID $WORKER_PID"

# Dashboard in foreground — primary process for Railway HTTP health.
exec python monitoring/dashboard.py
