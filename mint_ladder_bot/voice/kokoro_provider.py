"""
Kokoro TTS provider — optional higher-quality, more natural voice.
Uses kokoro-tts package (pip install kokoro-tts) when available.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import List, Optional

from .tts_provider_base import TTSProvider

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "af_heart"  # common default in kokoro-tts


class KokoroProvider(TTSProvider):
    """Kokoro TTS: optional; requires kokoro-tts and model download."""

    def __init__(self) -> None:
        self._engine: Optional[object] = None
        self._init_engine()

    def _init_engine(self) -> None:
        try:
            from kokoro import KPipeline  # type: ignore
            self._engine = KPipeline(lang_code="a")  # American English
        except ImportError:
            self._engine = None
        except Exception as e:
            logger.debug("Kokoro KPipeline init failed: %s", e)
            self._engine = None

    @classmethod
    def available(cls) -> bool:
        try:
            from kokoro import KPipeline  # type: ignore
            return True
        except ImportError:
            return False
        except Exception:
            return False

    def list_voices(self) -> List[str]:
        # Common Kokoro voices
        return ["af_heart", "am_michael", "af_bella", "am_adam", DEFAULT_VOICE]

    def synthesize(self, text: str, voice: str | None = None) -> Path | None:
        if self._engine is None or not text or not text.strip():
            return None
        voice_id = (voice or DEFAULT_VOICE).strip() or DEFAULT_VOICE
        out_path: Optional[Path] = None
        try:
            import soundfile as sf  # type: ignore
            out_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            out_path = Path(out_file.name)
            out_file.close()
            chunks = []
            for _gs, _ps, audio in self._engine(text.strip(), voice=voice_id):
                chunks.append(audio)
            if not chunks:
                return None
            import numpy as np
            audio_out = np.concatenate(chunks)
            sf.write(str(out_path), audio_out, 24000)
            if out_path.exists():
                return out_path
        except Exception as e:
            logger.warning("Kokoro synthesize failed: %s", e)
        if out_path and out_path.exists():
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
        return None

    def speak(self, text: str, voice: str | None = None) -> None:
        path = self.synthesize(text, voice)
        if path is None:
            return
        try:
            from .piper_provider import _play_wav
            _play_wav(path)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
