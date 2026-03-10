#!/usr/bin/env python3
"""
Notify founder: Telegram is the PRIMARY completion channel when configured; Mac is secondary.
Throttling: max 3 non-critical notifications per minute; critical (runtime unhealthy, restart failure, incident) bypass.
Usage:
  python tools/notify_founder.py "Short message"
  python tools/notify_founder.py "Message" "Custom title"
  python tools/notify_founder.py --critical "Runtime unhealthy — investigation started"
"""
from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path

MAX_PER_MINUTE = 3
WINDOW_SEC = 60

logger = logging.getLogger(__name__)


def _send_telegram(msg: str) -> bool:
    """Send msg to Telegram. Returns True if sent successfully."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    try:
        import urllib.request
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": msg}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def _write_telegram_alert(project_root: Path, message_preview: str) -> None:
    """Log failure and append to notification_alert.jsonl."""
    logger.warning("Telegram delivery failed after retry: %s", message_preview[:80])
    alert_dir = project_root / "runtime"
    alert_dir.mkdir(parents=True, exist_ok=True)
    alert_file = alert_dir / "notification_alert.jsonl"
    try:
        with open(alert_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "event": "telegram_delivery_failed",
                "message_preview": message_preview[:80],
            }) + "\n")
    except Exception:
        pass


def notify_founder(message: str, title: str = "Mint Ladder", project_root: Path | None = None) -> None:
    """Send notification to founder: Telegram first (primary when configured), then Mac (secondary). No secrets."""
    if not message or not message.strip():
        return
    msg = " ".join(str(message).split())[:500]

    # Telegram (PRIMARY completion channel when configured)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        if _send_telegram(msg):
            pass  # success
        else:
            _time.sleep(1)
            if not _send_telegram(msg):
                root = project_root or Path.cwd()
                _write_telegram_alert(root, msg)

    # macOS (secondary; optional)
    if platform.system() == "Darwin":
        try:
            esc = msg.replace("\\", "\\\\").replace('"', '\\"')
            tit = title.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e", f'display notification "{esc}" with title "{tit}"'],
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            pass


def _throttle_file(cwd: Path) -> Path:
    p = os.environ.get("NOTIFY_THROTTLE_FILE")
    if p:
        return Path(p)
    return cwd / "runtime" / "notify_throttle.json"


def _should_throttle(cwd: Path) -> bool:
    """True if we should skip (already at max in window)."""
    path = _throttle_file(cwd)
    now = datetime.now(tz=timezone.utc)
    cutoff = now.timestamp() - WINDOW_SEC
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            ts_list = [t for t in (data.get("timestamps") or []) if t >= cutoff]
        else:
            ts_list = []
    except Exception:
        ts_list = []
    return len(ts_list) >= MAX_PER_MINUTE


def _record_sent(cwd: Path) -> None:
    path = _throttle_file(cwd)
    now = datetime.now(tz=timezone.utc)
    cutoff = now.timestamp() - WINDOW_SEC
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            ts_list = [t for t in (data.get("timestamps") or []) if t >= cutoff]
        else:
            ts_list = []
        ts_list.append(now.timestamp())
        path.write_text(json.dumps({"timestamps": ts_list}, indent=0), encoding="utf-8")
    except Exception:
        pass


def notify_founder_throttled(
    message: str,
    title: str = "Mint Ladder",
    critical: bool = False,
    cwd: Path | None = None,
) -> bool:
    """
    Send notification with throttling (max 3/min). Critical bypasses throttle.
    Returns True if notification was sent, False if throttled or empty message.
    """
    if not message or not message.strip():
        return False
    cwd = cwd or Path.cwd()
    if not critical and _should_throttle(cwd):
        return False
    notify_founder(message, title, project_root=cwd)
    if not critical:
        _record_sent(cwd)
    return True


def main() -> None:
    args = sys.argv[1:]
    critical = False
    if args and args[0] == "--critical":
        critical = True
        args = args[1:]
    if len(args) < 1:
        print("Usage: notify_founder.py [--critical] \"message\" [title]", file=sys.stderr)
        sys.exit(1)
    message = args[0]
    title = args[1] if len(args) > 1 else os.environ.get("NOTIFY_TITLE", "Mint Ladder")
    cwd = Path.cwd()
    if critical:
        notify_founder(message, title, project_root=cwd)
    else:
        if _should_throttle(cwd):
            sys.exit(0)
        notify_founder(message, title, project_root=cwd)
        _record_sent(cwd)
    sys.exit(0)


if __name__ == "__main__":
    main()
