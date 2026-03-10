"""
Quiet hours: optional window when non-critical TTS is suppressed.
Config: TTS_QUIET_HOURS_START=23:00, TTS_QUIET_HOURS_END=07:00.
Critical events (RPC_FAILOVER, GLOBAL_PAUSE, CRITICAL_WARNING) are allowed.
"""
from __future__ import annotations

import os
from datetime import datetime, time
from typing import Optional, Tuple

# Event types that are always allowed during quiet hours
CRITICAL_EVENT_TYPES = frozenset({"RPC_FAILOVER", "GLOBAL_PAUSE", "CRITICAL_WARNING"})


def _parse_time(s: str) -> Optional[time]:
    """Parse HH:MM or HH:MM:SS to time. Returns None if invalid."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def get_quiet_hours() -> Optional[Tuple[time, time]]:
    """
    Return (start, end) as time objects if configured, else None.
    End is exclusive (e.g. 07:00 means up to but not including 07:00).
    """
    start_s = os.getenv("TTS_QUIET_HOURS_START", "").strip()
    end_s = os.getenv("TTS_QUIET_HOURS_END", "").strip()
    if not start_s or not end_s:
        return None
    start = _parse_time(start_s)
    end = _parse_time(end_s)
    if start is None or end is None:
        return None
    return (start, end)


def is_quiet_hours(now: Optional[datetime] = None) -> bool:
    """True if current time (or given now) falls inside the quiet-hours window."""
    window = get_quiet_hours()
    if window is None:
        return False
    start, end = window
    t = (now or datetime.now()).time()
    if start <= end:  # e.g. 09:00 - 17:00
        return start <= t < end
    # e.g. 23:00 - 07:00 (overnight)
    return t >= start or t < end


def should_suppress_for_quiet_hours(critical: bool, event_type: Optional[str] = None) -> bool:
    """
    True if we should suppress this message (quiet hours and not allowed).
    Critical events (critical=True or event_type in CRITICAL_EVENT_TYPES) are not suppressed.
    """
    if not is_quiet_hours():
        return False
    if critical:
        return False
    if event_type and event_type in CRITICAL_EVENT_TYPES:
        return False
    return True
