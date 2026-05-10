#!/bin/bash
cd "$(dirname "$0")"
set -e
# Railway HTTP URL: prefer Railway's RAILWAY_PUBLIC_DOMAIN or set PUBLIC_URL yourself,
# e.g. https://quant-production-4569.up.railway.app — app code should read env, not hardcode hosts.
# Worker in background: failures must not stop the dashboard (Railway healthchecks /health).
python main_worker.py || echo "worker exited (non-fatal for dashboard)" &
WORKER_PID=$!
echo "Worker started with PID $WORKER_PID"

# Dashboard in foreground — primary process for Railway HTTP health.
exec python monitoring/dashboard.py
