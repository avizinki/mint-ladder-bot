#!/usr/bin/env bash
# Restart dashboard only (no changes to runner or runtime files).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[restart_dashboard] Stopping dashboard..."
"$SCRIPT_DIR/stop_dashboard.sh"

echo "[restart_dashboard] Starting dashboard..."
"$SCRIPT_DIR/start_dashboard.sh"

echo "[restart_dashboard] Dashboard restart complete."

