#!/usr/bin/env bash
# Run from Railway Cron or your laptop. Cwd = quantbot (package root).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."
exec python -m training.cross_asset_tune "$@"
