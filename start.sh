#!/bin/bash
cd "$(dirname "$0")"
set -e
# Start the trading worker in background
python main_worker.py &
WORKER_PID=$!
echo "Worker started with PID $WORKER_PID"

# Start the Flask dashboard in foreground
python monitoring/dashboard.py
