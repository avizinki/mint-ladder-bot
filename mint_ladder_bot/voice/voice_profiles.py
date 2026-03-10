"""
Voice profile mapping: subsystem roles to TTS voice identifiers.
Configurable via env (VOICE_PROFILE_CTO, VOICE_PROFILE_DEVOPS, etc.);
sensible defaults when not defined.
"""
from __future__ import annotations

import os
from typing import Optional

# Subsystem keys used by telegram_events and voice_router
SUBSYSTEMS = (
    "CTO",
    "DevOps",
    "Backend",
    "Data",
    "Execution Engine",
    "Risk Engine",
    "Dashboard",
    "QA",
    "Launch Detector",
    "Sniper Engine",
    "Ladder Engine",
)

_ENV_PREFIX = "VOICE_PROFILE_"

# Default voice profile ids when env not set. Two Piper voices for distinct role sound: lessac (female), ryan (male).
# Intent: CTO/Risk/QA/Ladder = lessac; DevOps/Execution/Launch/Sniper = ryan. Fallback to lessac if ryan missing.
DEFAULT_PROFILES = {
    "CTO": "en_US-lessac-medium",
    "DEVOPS": "en_US-ryan-medium",
    "EXECUTION_ENGINE": "en_US-ryan-medium",
    "RISK_ENGINE": "en_US-lessac-medium",
    "QA": "en_US-lessac-medium",
    "BACKEND": "en_US-ryan-medium",
    "DATA": "en_US-lessac-medium",
    "DASHBOARD": "en_US-lessac-medium",
    "LAUNCH_DETECTOR": "en_US-ryan-medium",
    "SNIPER_ENGINE": "en_US-ryan-medium",
    "LADDER_ENGINE": "en_US-lessac-medium",
}


def get_voice_for_subsystem(subsystem: str) -> Optional[str]:
    """
    Return configured voice id for subsystem, or default, or None for provider default.
    Env: VOICE_PROFILE_CTO, VOICE_PROFILE_DEVOPS, VOICE_PROFILE_EXECUTION_ENGINE, etc.
    """
    key = subsystem.strip().upper().replace(" ", "_")
    env_name = f"{_ENV_PREFIX}{key}"
    val = os.getenv(env_name, "").strip()
    if val:
        return val
    return DEFAULT_PROFILES.get(key)


def get_voice_for_event_category(category: str) -> Optional[str]:
    """
    Map event category to voice. Categories: startup, buy_confirmed, sell_confirmed,
    ladder_triggered, rpc_failure, rebuild_started, rebuild_completed, critical_warning,
    founder_summary_ready.
    Env: VOICE_PROFILE_STARTUP, etc. Optional; no subsystem defaults for categories.
    """
    key = category.strip().lower().replace(" ", "_")
    env_name = f"{_ENV_PREFIX}{key}"
    val = os.getenv(env_name, "").strip()
    return val if val else None
