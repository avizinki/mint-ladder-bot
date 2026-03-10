"""
Local TTS voice layer — operator announcements only.
Provider abstraction (Piper default, Kokoro optional), queue, profiles.
"""
from __future__ import annotations

from .tts_manager import TTSManager, speak

__all__ = ["TTSManager", "speak"]
