"""
Voice layer telemetry: in-memory counters for summary and ops.
Thread-safe; never persisted (runtime only).
"""
from __future__ import annotations

import threading
from typing import Any, Dict

_lock = threading.Lock()
_messages_spoken = 0
_messages_suppressed = 0
_queue_overflows = 0


def increment_spoken() -> None:
    with _lock:
        global _messages_spoken
        _messages_spoken += 1


def increment_suppressed() -> None:
    with _lock:
        global _messages_suppressed
        _messages_suppressed += 1


def increment_queue_overflows() -> None:
    with _lock:
        global _queue_overflows
        _queue_overflows += 1


def get_telemetry() -> Dict[str, Any]:
    """Return current counters for summary_engine; safe to call from any thread."""
    with _lock:
        return {
            "voice_messages_spoken": _messages_spoken,
            "voice_messages_suppressed": _messages_suppressed,
            "voice_queue_overflows": _queue_overflows,
        }


def reset_telemetry() -> None:
    """Reset counters (e.g. for tests)."""
    with _lock:
        global _messages_spoken, _messages_suppressed, _queue_overflows
        _messages_spoken = 0
        _messages_suppressed = 0
        _queue_overflows = 0
