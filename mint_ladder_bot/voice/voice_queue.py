"""
Non-blocking voice queue: rate-limited, duplicate suppression, quiet hours, critical interrupt.
Never blocks trading runtime; TTS failures are logged and ignored.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .quiet_hours import should_suppress_for_quiet_hours
from .voice_telemetry import increment_queue_overflows, increment_spoken, increment_suppressed

logger = logging.getLogger(__name__)


class VoiceQueue:
    """
    Queue for TTS phrases. Worker thread consumes and speaks/writes.
    - Duplicate suppression: same text within cooldown is dropped.
    - Rate limit: min delay between plays (TTS_COOLDOWN_SECONDS).
    - Max size: drop oldest when full (TTS_MAX_QUEUE); counts as queue overflow.
    - Quiet hours: non-critical playback suppressed; suppressed counter incremented.
    - Critical interrupt: critical=True clears queue and plays immediately in caller thread, then resume.
    """

    def __init__(
        self,
        speak_fn: Callable[[str, Optional[str]], None],
        cooldown_seconds: float = 2.0,
        max_queue_size: int = 20,
        duplicate_cooldown_seconds: float = 30.0,
        output_dir: Optional[Path] = None,
        write_file: bool = False,
    ) -> None:
        self._speak_fn = speak_fn
        self._cooldown_seconds = max(0.1, cooldown_seconds)
        self._max_queue_size = max(1, max_queue_size)
        self._duplicate_cooldown = max(0, duplicate_cooldown_seconds)
        self._output_dir = output_dir
        self._write_file = write_file and output_dir is not None
        self._q: queue.Queue = queue.Queue(maxsize=self._max_queue_size)
        self._last_spoke_at = 0.0
        self._last_text: Optional[str] = None
        self._last_text_at = 0.0
        self._stopped = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def enqueue(
        self,
        text: str,
        voice: Optional[str] = None,
        critical: bool = False,
        event_type: Optional[str] = None,
    ) -> bool:
        """
        Enqueue phrase, or if critical=True: clear queue, play immediately, return.
        Returns True if accepted (or played), False if dropped (duplicate or full).
        """
        if not text or not text.strip():
            return False
        text = text.strip()

        # Critical interrupt: clear queue and play now in this thread
        if critical:
            self._clear_pending()
            try:
                self._speak_fn(text, voice)
                increment_spoken()
            except Exception as e:
                logger.warning("Voice critical play failed: %s", e)
            return True

        # Quiet hours: suppress non-critical
        if should_suppress_for_quiet_hours(critical=False, event_type=event_type):
            increment_suppressed()
            logger.debug("Voice: suppressed during quiet hours (non-critical)")
            return False

        now = time.monotonic()
        if self._duplicate_cooldown > 0 and self._last_text == text:
            if now - self._last_text_at < self._duplicate_cooldown:
                logger.debug("Voice queue: duplicate suppressed (cooldown): %.0fs", self._duplicate_cooldown)
                increment_suppressed()
                return False
        try:
            self._q.put_nowait((text, voice, False, event_type))
            return True
        except queue.Full:
            increment_queue_overflows()
            logger.debug("Voice queue full; dropping oldest or current")
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait((text, voice, False, event_type))
                return True
            except queue.Full:
                increment_queue_overflows()
                return False

    def _clear_pending(self) -> None:
        """Drain queue (drops pending items; can count as suppressed if desired)."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                item = self._q.get(timeout=0.5)
                if item is None:
                    break
                text, voice, _critical, event_type = item
                # Quiet hours: skip non-critical playback in worker
                if should_suppress_for_quiet_hours(critical=False, event_type=event_type):
                    increment_suppressed()
                    continue
                now = time.monotonic()
                wait = self._cooldown_seconds - (now - self._last_spoke_at)
                if wait > 0:
                    time.sleep(wait)
                try:
                    self._speak_fn(text, voice)
                    increment_spoken()
                except Exception as e:
                    logger.warning("Voice queue speak failed: %s", e)
                self._last_spoke_at = time.monotonic()
                self._last_text = text
                self._last_text_at = self._last_spoke_at
            except queue.Empty:
                continue
            except Exception as e:
                logger.debug("Voice queue worker: %s", e)

    def _write_wav_to_dir(self, text: str, voice: Optional[str]) -> None:
        """Optional: write synthesized WAV to output_dir. Requires speak_fn to support write."""
        pass

    def stop(self) -> None:
        self._stopped.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
