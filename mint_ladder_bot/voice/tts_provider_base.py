"""
TTS provider interface — local, offline, no cloud.
All providers implement available(), synthesize(), speak(), list_voices().
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List


class TTSProvider(ABC):
    """Abstract base for Piper, Kokoro, or future local TTS engines."""

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """True if this provider is installed and usable on this machine."""
        ...

    @abstractmethod
    def synthesize(self, text: str, voice: str | None = None) -> Path | None:
        """
        Convert text to WAV file. Returns path to WAV or None on failure.
        Caller must not assume file persists; copy if needed.
        """
        ...

    @abstractmethod
    def speak(self, text: str, voice: str | None = None) -> None:
        """Synthesize and play through local audio. Non-blocking preferred."""
        ...

    @abstractmethod
    def list_voices(self) -> List[str]:
        """Return list of voice identifiers this provider supports."""
        ...
