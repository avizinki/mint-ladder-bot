#!/usr/bin/env python3
"""
Runtime health check for mint-ladder-bot and optional dashboard HTTP (port from PORT_DASHBOARD_HTTP or registry default 6200).

Checks:
- HTTP reachability of localhost:8765 (optional, can skip with --no-8765)
- mint_ladder_bot.main run process running
- Optional: run.log has recent activity (Cycle summary within max age)
- Optional: RPC endpoint (from RPC_ENDPOINT env): success and latency; no endpoint URL in output.

Writes JSON status to file; exit 0 = healthy, non-zero = unhealthy.
No secrets in output.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mint_ladder_bot.runtime_paths import get_health_status_path, get_run_log_path


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def check_port_8765(host: str, port: int, path: str, timeout_sec: float) -> bool:
    """Return True if HTTP GET to host:port/path succeeds within timeout."""
    url = f"http://{host}:{port}{path}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_sec) as _:
            return True
    except Exception:
        return False


def check_bot_process() -> bool:
    """Return True if at least one mint_ladder_bot.main run process exists."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "mint_ladder_bot.main run"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.returncode == 0 and bool(out.stdout and out.stdout.strip())
    except Exception:
        return False


def check_rpc(endpoint: str, timeout_sec: float) -> tuple[bool, float]:
    """Return (success, latency_ms). Does not log or include endpoint in output."""
    if not endpoint or not endpoint.strip():
        return True, 0.0
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": []}
    ).encode("utf-8")
    start = time.monotonic()
    try:
        req = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if resp.status != 200:
                return False, (time.monotonic() - start) * 1000.0
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("error"):
                return False, (time.monotonic() - start) * 1000.0
    except Exception:
        return False, (time.monotonic() - start) * 1000.0
    return True, (time.monotonic() - start) * 1000.0


def check_log_progress(log_path: Path, max_age_min: int) -> bool:
    """Return True if log_path has a 'Cycle' summary line within last max_age_min minutes."""
    if not log_path.exists() or max_age_min <= 0:
        return True
    try:
        mtime = log_path.stat().st_mtime
        age_min = (datetime.now(timezone.utc).timestamp() - mtime) / 60.0
        if age_min > max_age_min:
            return False
        # Optionally scan last N lines for "Cycle ... summary"
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        if "Cycle " in text and "summary" in text:
            return True
        return age_min <= 2  # very recent write is enough
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Runtime health check for mint-ladder-bot and dashboard (port from env or 6200)")
    parser.add_argument("--status-file", type=Path, default=None, help="Write JSON status here (default: centralized runtime health_status.json)")
    parser.add_argument("--no-8765", action="store_true", help="Skip localhost:8765 check")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT_DASHBOARD_HTTP") or os.environ.get("PORT_8765") or "6200"), help="Port for dashboard HTTP (default: registry 6200 or PORT_DASHBOARD_HTTP)")
    parser.add_argument("--host", type=str, default=os.environ.get("HEALTH_CHECK_HOST", "127.0.0.1"), help="Host for local service")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout seconds")
    parser.add_argument("--log", type=Path, default=None, help="Path to run.log for progress check")
    parser.add_argument("--max-log-age-min", type=int, default=0, help="If >0, require log activity within this many minutes")
    parser.add_argument("--alerts-file", type=Path, default=None, help="If set and unhealthy, append uptime alerts (one JSON line each)")
    parser.add_argument("--no-rpc", action="store_true", help="Skip RPC endpoint check")
    args = parser.parse_args()

    status_path = args.status_file or get_health_status_path()
    check_8765 = not args.no_8765

    port_ok = check_port_8765(args.host, args.port, "/", args.timeout) if check_8765 else True
    bot_ok = check_bot_process()
    log_ok = check_log_progress(args.log or get_run_log_path(), args.max_log_age_min) if args.max_log_age_min > 0 else True
    rpc_ok, rpc_latency_ms = (True, 0.0)
    if not args.no_rpc and os.environ.get("RPC_ENDPOINT", "").strip():
        rpc_ok, rpc_latency_ms = check_rpc(
            os.environ.get("RPC_ENDPOINT", "").strip(),
            args.timeout,
        )

    healthy = port_ok and bot_ok and log_ok and rpc_ok
    status = {
        "ok": healthy,
        "checks": {
            "port_8765": port_ok,
            "bot_process": bot_ok,
            "log_progress": log_ok,
            "rpc": {"ok": rpc_ok, "latency_ms": round(rpc_latency_ms, 2)},
        },
        "timestamp": _now_utc(),
    }
    try:
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"Warning: could not write status file: {e}", file=sys.stderr)

    if not healthy and args.alerts_file:
        try:
            with open(args.alerts_file, "a", encoding="utf-8") as f:
                if check_8765 and not port_ok:
                    f.write(json.dumps({"alert_type": "service_8765_down", "severity": "high", "message": "localhost:8765 not responding", "timestamp": _now_utc()}) + "\n")
                if not bot_ok:
                    f.write(json.dumps({"alert_type": "bot_process_missing", "severity": "high", "message": "mint_ladder_bot process not running", "timestamp": _now_utc()}) + "\n")
                if args.log and not log_ok:
                    f.write(json.dumps({"alert_type": "log_stale", "severity": "medium", "message": "No recent cycle in run.log", "timestamp": _now_utc()}) + "\n")
                if not rpc_ok:
                    f.write(json.dumps({"alert_type": "rpc_unhealthy", "severity": "high", "message": "RPC endpoint check failed", "timestamp": _now_utc()}) + "\n")
        except Exception as e:
            print(f"Warning: could not write alerts file: {e}", file=sys.stderr)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
