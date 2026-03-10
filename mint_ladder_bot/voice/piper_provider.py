"""
Piper TTS provider — fast, local, operationally reliable.
Uses piper binary (from PATH or PIPER_PATH) or piper-tts Python package.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from mint_ladder_bot.runtime_paths import get_project_runtime_dir
from .tts_provider_base import TTSProvider

logger = logging.getLogger(__name__)

# Default voice model name (without .onnx); installer downloads at least one
DEFAULT_VOICE = "en_US-lessac-medium"


def _piper_binary_path() -> Optional[Path]:
    import os
    explicit = os.getenv("PIPER_PATH", "").strip()
    if explicit:
        p = Path(explicit)
        if p.is_file() or (p / "piper").is_file():
            return p if p.name == "piper" else p / "piper"
    # Check PATH
    import shutil
    which = shutil.which("piper")
    if which:
        return Path(which)
    return None


def _piper_models_dir() -> Path:
    # Piper voice models are runtime assets; they must live under the
    # centralized runtime/projects/mint_ladder_bot tree, not the project root.
    return get_project_runtime_dir() / "voice" / "voices" / "piper"


def _find_voice_model(voice: str | None) -> Optional[Path]:
    voice = (voice or DEFAULT_VOICE).strip() or DEFAULT_VOICE
    models_dir = _piper_models_dir()
    # voice may be "en_US-lessac-medium" -> en_US-lessac-medium.onnx
    base = voice if voice.endswith(".onnx") else voice
    onnx = models_dir / base if base.endswith(".onnx") else models_dir / f"{base}.onnx"
    if onnx.exists():
        return onnx
    # Try first .onnx in dir as fallback
    for f in models_dir.glob("*.onnx"):
        return f
    return None


class PiperProvider(TTSProvider):
    """Piper TTS: binary or piper-tts package."""

    def __init__(self, models_dir: Optional[Path] = None) -> None:
        self._models_dir = models_dir or _piper_models_dir()
        self._binary = _piper_binary_path()
        self._python_available = False
        try:
            import piper  # type: ignore
            self._piper_module = piper
            self._python_available = True
        except ImportError:
            self._piper_module = None

    @classmethod
    def available(cls) -> bool:
        if _piper_binary_path() is not None:
            return _find_voice_model(None) is not None
        try:
            import piper  # type: ignore
            return _find_voice_model(None) is not None
        except ImportError:
            pass
        return False

    def list_voices(self) -> List[str]:
        out: List[str] = []
        if not self._models_dir.exists():
            return out
        for f in self._models_dir.glob("*.onnx"):
            name = f.stem
            if name and name not in out:
                out.append(name)
        if not out and self._python_available and self._piper_module is not None:
            try:
                voices = getattr(self._piper_module, "list_voices", None)
                if callable(voices):
                    out = list(voices()) or []
            except Exception:
                pass
        return sorted(out) if out else [DEFAULT_VOICE]

    def synthesize(self, text: str, voice: str | None = None) -> Path | None:
        if not text or not text.strip():
            return None
        model_path = _find_voice_model(voice)
        if not model_path:
            logger.warning("Piper: no voice model found for %s", voice or DEFAULT_VOICE)
            return None
        try:
            out_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            out_path = Path(out_file.name)
            out_file.close()
        except OSError as e:
            logger.warning("Piper: failed to create temp file: %s", e)
            return None

        # Prefer binary for stability
        binary = _piper_binary_path()
        if binary:
            try:
                proc = subprocess.run(
                    [str(binary), "--model", str(model_path), "--output_file", str(out_path)],
                    input=text.strip().encode("utf-8"),
                    capture_output=True,
                    timeout=30,
                    cwd=str(self._models_dir.parent),
                )
                if proc.returncode == 0 and out_path.exists():
                    return out_path
                if proc.stderr:
                    logger.debug("Piper binary stderr: %s", proc.stderr.decode("utf-8", errors="replace")[:500])
            except subprocess.TimeoutExpired:
                logger.warning("Piper: synthesis timed out")
            except Exception as e:
                logger.warning("Piper binary failed: %s", e)
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

        # Python piper-tts (1.4+ API: PiperVoice.load, synthesize_wav with wave.Wave_write)
        if self._python_available and self._piper_module is not None and model_path.exists():
            try:
                import wave
                PiperVoice = getattr(self._piper_module, "PiperVoice", None)
                if PiperVoice is not None:
                    voice_instance = PiperVoice.load(str(model_path))
                    with wave.open(str(out_path), "wb") as wav_file:
                        voice_instance.synthesize_wav(text.strip(), wav_file)
                    if out_path.exists():
                        return out_path
                else:
                    # Legacy: Voice.from_config + synthesize (old piper API)
                    voice_obj = getattr(self._piper_module, "Voice", None)
                    if voice_obj:
                        v = voice_obj.from_config(model_path)
                        v.synthesize(text.strip(), out_path)
                        if out_path.exists():
                            return out_path
            except Exception as e:
                logger.warning("Piper Python synthesize failed: %s", e)
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
            _play_wav(path)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _play_wav(path: Path) -> None:
    """Play WAV file using platform default (afplay on macOS)."""
    import subprocess
    import sys
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["afplay", str(path)],
                capture_output=True,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            logger.debug("afplay failed: %s", e)
    else:
        try:
            subprocess.run(
                ["aplay", str(path)],
                capture_output=True,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            logger.debug("aplay failed: %s", e)
