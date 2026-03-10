#!/usr/bin/env python3
"""
Watchdog for mint-ladder-bot runtime and dashboard (port from PORT_DASHBOARD_HTTP or registry 6200).

Runs health check and config-hash check periodically; on health failure or
config change attempts restart via restart_runtime.sh; on repeated restart
failure writes escalation and creates STOP file. Uses backoff; no infinite tight loop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mint_ladder_bot.runtime_paths import get_health_status_path, get_run_log_path, get_runtime_root


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def env_hash(env_path: Path) -> str:
    """Compute SHA256 hash of .env file content."""
    if not env_path.exists():
        return ""
    data = env_path.read_bytes()
    return hashlib.sha256(data).hexdigest()


def read_stored_hash(runtime_dir: Path) -> str | None:
    """Read stored env_hash from runtime/config_hash.json. Return None if missing or invalid."""
    path = runtime_dir / "config_hash.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("env_hash") or None
    except Exception:
        return None


def write_config_hash(runtime_dir: Path, env_path: Path) -> None:
    """Write current .env hash to runtime/config_hash.json."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    h = env_hash(env_path)
    path = runtime_dir / "config_hash.json"
    path.write_text(
        json.dumps({"env_hash": h, "timestamp": _now_utc()}, indent=2),
        encoding="utf-8",
    )


def config_changed(project_root: Path) -> bool:
    """True if .env hash differs from stored hash. Ensures stored hash exists on first run."""
    env_path = project_root / ".env"
    runtime_dir = get_runtime_root()
    current = env_hash(env_path)
    stored = read_stored_hash(runtime_dir)
    if stored is None:
        write_config_hash(runtime_dir, env_path)
        return False
    return current != "" and current != stored


def log_config_change_detected(project_root: Path) -> None:
    """Log CONFIG_CHANGE_DETECTED to runtime/config_watch.log."""
    log_path = get_runtime_root() / "config_watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{_now_utc()}] CONFIG_CHANGE_DETECTED\n"
    try:
        log_path.write_text(log_path.read_text(encoding="utf-8") + line, encoding="utf-8")
    except Exception:
        log_path.write_text(line, encoding="utf-8")
    print(line.strip(), file=sys.stderr)


def run_health_check(project_root: Path, no_8765: bool, log_path: Path | None, max_log_age: int) -> bool:
    """Run check_runtime_health.py; return True if healthy."""
    script = project_root / "tools" / "check_runtime_health.py"
    if not script.exists():
        return False
    cmd = [sys.executable, str(script), "--status-file", str(get_health_status_path())]
    if no_8765:
        cmd.append("--no-8765")
    if log_path:
        cmd.extend(["--log", str(log_path), "--max-log-age-min", str(max_log_age)])
    try:
        result = subprocess.run(cmd, cwd=str(project_root), capture_output=True, timeout=30)
        return result.returncode == 0
    except Exception:
        return False


def run_restart(
    project_root: Path,
    restart_script: Path,
    override_stop: bool,
    reason: str | None = None,
    live: bool = False,
) -> bool:
    """Run restart script; return True if exit 0."""
    if not restart_script.exists():
        return False
    cmd = ["/bin/bash", str(restart_script)]
    if override_stop:
        cmd.append("--override-stop")
    env = os.environ.copy()
    if reason:
        env["RESTART_REASON"] = reason
    if live:
        env["RESTART_LIVE"] = "1"
    try:
        result = subprocess.run(cmd, cwd=str(project_root), env=env, capture_output=True, timeout=120)
        return result.returncode == 0
    except Exception:
        return False


def write_restart_verified(project_root: Path, verified: bool, reason: str = "env_hash_changed") -> None:
    """Append to runtime/restart_log.jsonl with verification result."""
    path = get_runtime_root() / "restart_log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "event": "config_restart",
        "reason": reason,
        "timestamp": _now_utc(),
        "verified": verified,
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def write_escalation(project_root: Path, failure_count: int, reason: str) -> None:
    """Append one line to escalation.jsonl."""
    path = get_runtime_root() / "escalation.jsonl"
    event = {
        "event": "restart_failed_repeatedly",
        "timestamp": _now_utc(),
        "failure_count": failure_count,
        "last_reason": reason,
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


def ensure_stop_file(project_root: Path) -> None:
    """Create STOP file in project root if not present."""
    stop_path = project_root / "STOP"
    if not stop_path.exists():
        try:
            stop_path.write_text("")
        except Exception:
            pass


def _unhealthy_notification_message(project_root: Path, no_8765: bool) -> str:
    """Build notification message from last health status; mention dashboard when 8765 is down."""
    if no_8765:
        return "Runtime unhealthy — investigation started"
    status_path = project_root / "health_status.json"
    try:
        if status_path.exists():
            data = json.loads(status_path.read_text(encoding="utf-8"))
            if not data.get("checks", {}).get("port_8765", True):
                return "Dashboard not responding — investigation started"
    except Exception:
        pass
    return "Runtime unhealthy — investigation started"


def _notify_founder_critical(project_root: Path, message: str, title: str = "Mint Ladder") -> None:
    """Run notify_founder.py with --critical (bypasses throttle). No-op if script missing."""
    script = project_root / "tools" / "notify_founder.py"
    if not script.exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(script), "--critical", message[:500], title],
            cwd=str(project_root),
            capture_output=True,
            timeout=10,
            env=os.environ.copy(),
        )
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Watchdog for mint-ladder-bot and dashboard (port 6200 or PORT_DASHBOARD_HTTP)")
    parser.add_argument("--project-root", type=Path, default=Path.cwd(), help="mint-ladder-bot project root")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("WATCHDOG_INTERVAL_SEC", "60")), help="Seconds between checks when healthy")
    parser.add_argument("--max-failures", type=int, default=int(os.environ.get("MAX_RESTART_ATTEMPTS", "3")), help="Restart attempts before escalation")
    parser.add_argument("--no-8765", action="store_true", help="Skip dashboard port check")
    parser.add_argument("--override-stop", action="store_true", help="Allow restart even if STOP exists (use with care)")
    parser.add_argument("--run-log", type=Path, default=None, help="Path to run.log for progress check")
    parser.add_argument("--max-log-age-min", type=int, default=10, help="Max minutes without log progress (0=disable)")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    restart_script = project_root / "tools" / "restart_runtime.sh"
    log_path = args.run_log or get_run_log_path()
    env_path = project_root / ".env"
    interval = max(10, args.interval)
    sleep_after_restart = 30
    consecutive_failures = 0

    # Ensure config hash exists on first run (so we have a baseline)
    if (get_runtime_root() / "config_hash.json").exists():
        pass
    elif env_path.exists():
        write_config_hash(get_runtime_root(), env_path)

    while True:
        # 1. Config change detection: if .env hash changed, restart with new config
        if config_changed(project_root):
            log_config_change_detected(project_root)
            ok = run_restart(
                project_root,
                restart_script,
                args.override_stop,
                reason="config_change",
                live=True,
            )
            time.sleep(sleep_after_restart)
            verified = run_health_check(
                project_root, args.no_8765, log_path if args.max_log_age_min > 0 else None, args.max_log_age_min
            )
            write_restart_verified(project_root, verified, reason="env_hash_changed")
            if verified:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= args.max_failures:
                    write_escalation(project_root, consecutive_failures, "config_restart_failed_repeatedly")
                    ensure_stop_file(project_root)
            time.sleep(interval)
            continue

        # 2. Health check
        healthy = run_health_check(project_root, args.no_8765, log_path if args.max_log_age_min > 0 else None, args.max_log_age_min)
        if healthy:
            consecutive_failures = 0
            time.sleep(interval)
            continue

        # Notify founder (critical; bypasses throttle). Mention dashboard if 8765 is down.
        msg = _unhealthy_notification_message(project_root, args.no_8765)
        _notify_founder_critical(project_root, msg)

        consecutive_failures += 1
        reason = "health_check_failed"
        if consecutive_failures <= args.max_failures:
            ok = run_restart(project_root, restart_script, args.override_stop)
            time.sleep(sleep_after_restart)
            if ok:
                if run_health_check(project_root, args.no_8765, log_path if args.max_log_age_min > 0 else None, args.max_log_age_min):
                    consecutive_failures = 0
                    continue
            reason = "restart_failed_or_still_unhealthy"
        else:
            write_escalation(project_root, consecutive_failures, reason)
            esc_msg = _unhealthy_notification_message(project_root, args.no_8765).replace(
                "— investigation started", "— escalation (restart failed repeatedly)"
            )
            _notify_founder_critical(project_root, esc_msg)
            ensure_stop_file(project_root)
            time.sleep(300)
            if run_health_check(project_root, args.no_8765, log_path if args.max_log_age_min > 0 else None, args.max_log_age_min):
                consecutive_failures = 0
                continue
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
