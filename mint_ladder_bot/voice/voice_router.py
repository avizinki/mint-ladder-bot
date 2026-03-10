"""
TTS provider selection: auto | piper | kokoro.
Prefers Piper for runtime stability when auto; falls back to text-only if unavailable.
"""
from __future__ import annotations

import logging
from typing import Optional

from .kokoro_provider import KokoroProvider
from .piper_provider import PiperProvider
from .tts_provider_base import TTSProvider

logger = logging.getLogger(__name__)


def get_tts_provider(choice: Optional[str] = None) -> Optional[TTSProvider]:
    """
    Return configured TTS provider or None (text-only mode).
    choice: "piper" | "kokoro" | "auto" | None (use TTS_PROVIDER env).
    """
    import os
    c = (choice or os.getenv("TTS_PROVIDER", "auto")).strip().lower()
    if c == "piper":
        if PiperProvider.available():
            return PiperProvider()
        logger.warning("TTS_PROVIDER=piper but Piper not available; falling back to text-only.")
        return None
    if c == "kokoro":
        if KokoroProvider.available():
            return KokoroProvider()
        logger.warning("TTS_PROVIDER=kokoro but Kokoro not available; falling back to text-only.")
        return None
    # auto: prefer Piper for stability
    if PiperProvider.available():
        return PiperProvider()
    if KokoroProvider.available():
        return KokoroProvider()
    logger.debug("TTS auto: no local provider available; text-only mode.")
    return None
