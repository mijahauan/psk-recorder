"""SlotWorker: extracts cadence-aligned WAV slots and invokes the decoder.

One SlotWorker per channel. Runs as a daemon thread, polling the ring
buffer every 500 ms for completed slots.

FT8 cadence: 15 s (slots at :00, :15, :30, :45)
FT4 cadence: 7.5 s (slots at :00, :07.5, :15, :22.5, :30, :37.5, :45, :52.5)
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from psk_recorder.core.ring import Ring
from psk_recorder.core.wav import write_wav

logger = logging.getLogger(__name__)

SETTLE_SEC = 1.5


class SlotWorker:
    """Extracts cadence-aligned audio slots from a Ring and decodes them."""

    def __init__(
        self,
        ring: Ring,
        mode: str,
        frequency_hz: int,
        cadence_sec: float,
        spool_dir: Path,
        log_fd,
        decoder_path: str,
        keep_wav: bool = False,
    ):
        self._ring = ring
        self._mode = mode
        self._frequency_hz = frequency_hz
        self._cadence_sec = cadence_sec
        self._spool_dir = spool_dir
        self._log_fd = log_fd
        self._decoder_path = decoder_path
        self._keep_wav = keep_wav
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._next_slot_start: Optional[float] = None
        self._pending_procs: list[tuple[subprocess.Popen, Path]] = []

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name=f"slot-{self._mode}-{self._frequency_hz}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        self._reap_all(wait=True)

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("SlotWorker tick error")
            time.sleep(0.5)

    def _tick(self) -> None:
        self._reap_finished()

        head = self._ring.head_utc()
        if head is None:
            return

        if self._next_slot_start is None:
            self._next_slot_start = self._last_completed_boundary(head)
            logger.info(
                "%s %d Hz: first slot at %.1f (head=%.1f)",
                self._mode.upper(), self._frequency_hz,
                self._next_slot_start, head,
            )
            return

        slot_end = self._next_slot_start + self._cadence_sec
        if head < slot_end + SETTLE_SEC:
            return

        samples = self._ring.extract_slot(
            self._next_slot_start, self._cadence_sec
        )
        if samples is None:
            logger.warning(
                "%s %d Hz: slot at %.1f — insufficient samples, skipping",
                self._mode.upper(), self._frequency_hz, self._next_slot_start,
            )
            self._next_slot_start = slot_end
            return

        wav_path = self._write_spool_wav(samples)
        self._fork_decoder(wav_path)

        self._next_slot_start = slot_end

    def _align_to_cadence(self, utc: float) -> float:
        """Find the next cadence boundary at or after utc."""
        cadence = self._cadence_sec
        return math.ceil(utc / cadence) * cadence

    def _last_completed_boundary(self, head_utc: float) -> float:
        """Find the start of the most recent slot whose end + settle <= head.

        This means: floor((head - settle - cadence) / cadence) * cadence,
        clamped so we never go negative. We start decoding from the most
        recently completed slot, not some future one.
        """
        cadence = self._cadence_sec
        latest_end = head_utc - SETTLE_SEC
        latest_start = latest_end - cadence
        if latest_start < 0:
            return 0.0
        return math.floor(latest_start / cadence) * cadence

    def _write_spool_wav(self, samples) -> Path:
        slot_time = time.gmtime(self._next_slot_start)
        freq_khz = self._frequency_hz // 1000
        filename = time.strftime("%y%m%d_%H%M%S", slot_time) + f"_{freq_khz}.wav"
        wav_path = self._spool_dir / filename

        write_wav(
            path=wav_path,
            samples=samples,
            sample_rate=self._ring.sample_rate,
            frequency_hz=self._frequency_hz,
        )
        return wav_path

    def _fork_decoder(self, wav_path: Path) -> None:
        freq_mhz = self._frequency_hz / 1e6
        cmd = [self._decoder_path, "-f", f"{freq_mhz:.6f}"]
        if self._mode == "ft4":
            cmd.append("-4")
        cmd.append(str(wav_path))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=self._log_fd,
                stderr=subprocess.PIPE,
            )
            self._pending_procs.append((proc, wav_path))
            logger.debug(
                "%s %d Hz: decode_ft8 pid=%d on %s",
                self._mode.upper(), self._frequency_hz, proc.pid, wav_path.name,
            )
        except OSError as exc:
            logger.error("Failed to launch decoder: %s", exc)
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)

    def _reap_finished(self) -> None:
        still_pending = []
        for proc, wav_path in self._pending_procs:
            ret = proc.poll()
            if ret is None:
                still_pending.append((proc, wav_path))
                continue
            if ret != 0:
                stderr = proc.stderr.read().decode(errors="replace").strip()[:200]
                logger.warning(
                    "decode_ft8 exit %d for %s: %s", ret, wav_path.name, stderr,
                )
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)
        self._pending_procs = still_pending

    def _reap_all(self, wait: bool = False) -> None:
        for proc, wav_path in self._pending_procs:
            if wait:
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)
        self._pending_procs.clear()
