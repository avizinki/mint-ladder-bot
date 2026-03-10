"""
Founder notifications: Telegram (optional). Never fail the bot.
Env: TG_BOT_TOKEN / TG_CHAT_ID / TG_NOTIFICATIONS_ENABLED (or TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).
Employee-style updates: notify_phase_done, notify_blocker, notify_founder.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Rate limit: max non-critical sends per window (same as tools/notify_founder.py)
_MAX_PER_MINUTE = 3
_WINDOW_SEC = 60
_last_sends: list[float] = []
_last_blocker_key: Optional[str] = None
_last_blocker_time: float = 0
_BLOCKER_DEBOUNCE_SEC = 300  # 5 min per distinct blocker


def _token() -> str:
    return (os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def _chat_id() -> str:
    return (os.environ.get("TG_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()


def _enabled() -> bool:
    v = (os.environ.get("TG_NOTIFICATIONS_ENABLED") or "").strip().lower()
    if v in ("false", "0", "no", "off"):
        return False
    if v in ("true", "1", "yes", "on"):
        return True
    # Default: enabled if token and chat_id set
    return bool(_token() and _chat_id())


def _sanitize_for_log(s: str, max_len: int = 20) -> str:
    """Never log tokens or full chat IDs."""
    if not s or len(s) < 4:
        return "***"
    return s[:2] + "***" + s[-2:] if len(s) > max_len else "***"


def _send_telegram(text: str) -> bool:
    """Send text to Telegram. Returns True on success. Never raises."""
    token = _token()
    chat_id = _chat_id()
    if not token or not chat_id:
        return False
    text = " ".join(str(text).split())[:500]
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        logger.warning("Telegram send failed (no secrets): %s", type(e).__name__)
        return False


def _should_throttle() -> bool:
    """True if we are at max non-critical sends in the window."""
    now = time.time()
    cutoff = now - _WINDOW_SEC
    global _last_sends
    _last_sends = [t for t in _last_sends if t >= cutoff]
    return len(_last_sends) >= _MAX_PER_MINUTE


def _record_sent() -> None:
    global _last_sends
    _last_sends.append(time.time())


def notify_founder(text: str, severity: str = "info") -> None:
    """
    Send a notification to the founder. Never raises; never fails the bot.
    severity: info | warning | critical. Critical bypasses rate limit.
    """
    if not text or not str(text).strip():
        return
    if not _enabled():
        logger.debug("Notifications disabled (TG_NOTIFICATIONS_ENABLED or missing TG_/TELEGRAM_ credentials)")
        return
    critical = severity == "critical"
    if not critical and _should_throttle():
        logger.debug("Notification throttled (max %d/min)", _MAX_PER_MINUTE)
        return
    ok = _send_telegram(str(text).strip()[:500])
    if not ok and critical:
        logger.warning("Critical notification delivery failed (Telegram unreachable or credentials invalid)")
    if not critical and ok:
        _record_sent()


def notify_phase_done(phase_name: str, summary: str, next_step: Optional[str] = None) -> None:
    """
    [EMPLOYEE UPDATE] style: phase done, 3 bullets max, next step.
    Rate-limited (non-critical).
    """
    lines = [f"[EMPLOYEE UPDATE]", f"Status: DONE", f"Phase: {phase_name}", f"What changed:", f"- {summary}"]
    if next_step:
        lines.append(f"Next: {next_step}")
    notify_founder("\n".join(lines), severity="info")


def notify_blocker(blocker: str, owner: Optional[str] = None) -> None:
    """
    Notify founder of a blocker. Debounced by blocker text (once per BLOCKER_DEBOUNCE_SEC).
    """
    global _last_blocker_key, _last_blocker_time
    key = (blocker.strip() or "")[:200]
    if not key:
        return
    now = time.time()
    if _last_blocker_key == key and (now - _last_blocker_time) < _BLOCKER_DEBOUNCE_SEC:
        logger.debug("Blocker notification debounced: %s", key[:50])
        return
    _last_blocker_key = key
    _last_blocker_time = now
    owner_line = f"Owner: {owner}" if owner else "Owner: unassigned"
    msg = f"[EMPLOYEE UPDATE]\nStatus: BLOCKED\n{owner_line}\nBlocker: {blocker.strip()[:300]}"
    notify_founder(msg, severity="warning")


def send_test_notification() -> bool:
    """
    Send one test message. Used to prove Telegram path. Returns True if sent.
    Safe to call; never raises.
    """
    if not _enabled():
        logger.info("Test notification skipped: TG_NOTIFICATIONS_ENABLED=false or credentials not set")
        return False
    msg = "[Cursor] Notifier test — Telegram path OK."
    ok = _send_telegram(msg)
    if ok:
        logger.info("Test notification sent to founder chat")
    else:
        logger.warning("Test notification failed (check TG_BOT_TOKEN / TG_CHAT_ID or network)")
    return ok
