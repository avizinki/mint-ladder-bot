"""
TTS manager: single entry point for voice output.
Wires provider, profiles, queue, output mode. Never speaks secrets; never blocks.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from mint_ladder_bot.runtime_paths import get_project_runtime_dir
from .voice_profiles import get_voice_for_subsystem
from .voice_queue import VoiceQueue
from .voice_router import get_tts_provider

logger = logging.getLogger(__name__)

# Phrases that must never be sent to TTS (secrets, keys, tokens)
_SECRET_SUBSTRINGS = ("key", "token", "secret", "password", "mnemonic", "private", "api_key")


def _safe_for_tts(text: str) -> bool:
    """False if text might contain secrets (best-effort filter)."""
    if not text or not text.strip():
        return False
    lower = text.lower()
    for sub in _SECRET_SUBSTRINGS:
        if sub in lower:
            # Allow e.g. "API ok" but not "api_key=..."
            if f"{sub}=" in lower or f"{sub}:" in lower or f" {sub} " in lower:
                return False
    return True


class TTSManager:
    """
    Facade: speak(text, sender=..., category=...) enqueues or speaks.
    Output mode: speak | file | both. Queue handles rate limit and duplicate suppression.
    """

    _instance: Optional["TTSManager"] = None

    def __init__(
        self,
        enabled: bool = True,
        output_mode: str = "speak",
        output_dir: Optional[Path] = None,
        cooldown_seconds: float = 2.0,
        max_queue_size: int = 20,
        duplicate_cooldown_seconds: float = 30.0,
    ) -> None:
        self._enabled = enabled
        self._output_mode = (output_mode or "speak").strip().lower()
        # Voice output files are runtime artifacts and must live under the
        # centralized runtime/projects/mint_ladder_bot tree, never under the
        # project source directory.
        self._output_dir = output_dir or (get_project_runtime_dir() / "voice")
        self._provider = get_tts_provider(None) if enabled else None
        self._queue: Optional[VoiceQueue] = None
        if self._provider and self._enabled:
            self._queue = VoiceQueue(
                speak_fn=self._do_speak,
                cooldown_seconds=cooldown_seconds,
                max_queue_size=max_queue_size,
                duplicate_cooldown_seconds=duplicate_cooldown_seconds,
                output_dir=self._output_dir if self._output_mode in ("file", "both") else None,
                write_file=(self._output_mode in ("file", "both")),
            )

    def _do_speak(self, text: str, voice: Optional[str] = None) -> None:
        """Called by queue worker: synthesize once, then play and/or write file."""
        if not self._provider or not _safe_for_tts(text):
            return
        try:
            wav_path = self._provider.synthesize(text, voice)
            if wav_path is None:
                return
            try:
                if self._output_mode in ("file", "both") and self._output_dir:
                    self._output_dir.mkdir(parents=True, exist_ok=True)
                    import time
                    dest = self._output_dir / f"tts_{int(time.time() * 1000)}.wav"
                    import shutil
                    shutil.copy2(wav_path, dest)
                if self._output_mode in ("speak", "both"):
                    from .piper_provider import _play_wav
                    _play_wav(wav_path)
            finally:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as e:
            logger.warning("TTS speak failed: %s", e)

    def speak(
        self,
        text: str,
        sender: Optional[str] = None,
        category: Optional[str] = None,
        critical: bool = False,
        event_type: Optional[str] = None,
    ) -> None:
        """
        Enqueue text for TTS. Uses sender/category to pick voice profile.
        event_type used for quiet-hours (critical events allowed) and telemetry.
        No-op if disabled, provider unavailable, or text not safe.
        """
        if not self._enabled or not self._provider or not _safe_for_tts(text):
            return
        voice = None
        if sender:
            voice = get_voice_for_subsystem(sender)
        if voice is None and category:
            from .voice_profiles import get_voice_for_event_category
            voice = get_voice_for_event_category(category)
        if self._queue:
            self._queue.enqueue(text, voice=voice, critical=critical, event_type=event_type)
        else:
            try:
                self._do_speak(text, voice)
            except Exception as e:
                logger.warning("TTS direct speak failed: %s", e)

    def is_available(self) -> bool:
        return self._enabled and self._provider is not None

    def list_voices(self) -> list:
        if not self._provider:
            return []
        return self._provider.list_voices()

    @classmethod
    def get_instance(cls, **kwargs: object) -> "TTSManager":
        if cls._instance is None:
            enabled = os.getenv("TTS_ENABLED", "false").strip().lower() in ("true", "1", "yes")
            output_mode = os.getenv("TTS_OUTPUT_MODE", "speak").strip().lower()
            # Voice output must always be under the centralized runtime tree.
            output_dir = get_project_runtime_dir() / "voice"
            cooldown = float(os.getenv("TTS_COOLDOWN_SECONDS", "2"))
            max_q = int(os.getenv("TTS_MAX_QUEUE", "20"))
            dup_cooldown = float(os.getenv("TTS_DUPLICATE_COOLDOWN_SECONDS", "30"))
            cls._instance = cls(
                enabled=enabled,
                output_mode=output_mode or "speak",
                output_dir=output_dir,
                cooldown_seconds=cooldown,
                max_queue_size=max_q,
                duplicate_cooldown_seconds=dup_cooldown,
                **kwargs,
            )
        return cls._instance


def speak(
    text: str,
    sender: Optional[str] = None,
    category: Optional[str] = None,
    critical: bool = False,
    event_type: Optional[str] = None,
) -> None:
    """Module-level convenience: enqueue TTS for text. Safe to call from anywhere."""
    TTSManager.get_instance().speak(text, sender=sender, category=category, critical=critical, event_type=event_type)


def synthesize_greeting_to_file(
    text: str,
    role: str,
    output_dir: Path,
) -> Optional[Path]:
    """
    Synthesize one greeting to a WAV file for upload (e.g. Telegram voice).
    Does not call speak(); does not enqueue. Uses TTS provider directly.
    output_dir: e.g. runtime/voice/outgoing. File: <timestamp>_<role_slug>_greeting.wav.
    Returns path to WAV file or None on failure.
    """
    if not text or not text.strip() or not _safe_for_tts(text):
        return None
    provider = get_tts_provider(None)
    if not provider:
        return None
    voice = get_voice_for_subsystem(role)
    try:
        wav_path = provider.synthesize(text.strip(), voice)
        if wav_path is None or not wav_path.exists():
            return None
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        import time
        role_slug = role.strip().replace(" ", "_").lower()
        safe_slug = "".join(c for c in role_slug if c.isalnum() or c == "_") or "role"
        dest = output_dir / f"{int(time.time() * 1000)}_{safe_slug}_greeting.wav"
        import shutil
        shutil.copy2(wav_path, dest)
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass
        return dest
    except Exception as e:
        logger.warning("synthesize_greeting_to_file failed: %s", e)
        return None
