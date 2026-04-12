"""ChannelStream: one ManagedStream + Ring + SlotWorker per channel.

Each ChannelStream handles a single frequency on a single radiod.
Uses ka9q-python's ManagedStream for automatic channel restoration
when the RTP stream drops (e.g., radiod restart). Pushes samples
into the ring buffer and delegates slot extraction + decoding to a
SlotWorker thread.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import numpy as np

from psk_recorder.config import FT4_CADENCE_SEC, FT8_CADENCE_SEC
from psk_recorder.core.ring import Ring
from psk_recorder.core.slot import SlotWorker

logger = logging.getLogger(__name__)

RING_SECONDS = 60.0


class ChannelStream:
    """Manages one channel: ManagedStream → Ring → SlotWorker → decoder."""

    def __init__(
        self,
        control,
        mode: str,
        frequency_hz: int,
        sample_rate: int,
        preset: str,
        encoding: int,
        radiod_id: str,
        spool_dir: Path,
        log_fd,
        decoder_path: str,
        keep_wav: bool = False,
    ):
        self._control = control
        self._mode = mode
        self._frequency_hz = frequency_hz
        self._sample_rate = sample_rate
        self._preset = preset
        self._encoding = encoding
        self._radiod_id = radiod_id

        cadence = FT4_CADENCE_SEC if mode == "ft4" else FT8_CADENCE_SEC

        self._ring = Ring(
            max_seconds=RING_SECONDS,
            sample_rate=sample_rate,
        )

        self._slot_worker = SlotWorker(
            ring=self._ring,
            mode=mode,
            frequency_hz=frequency_hz,
            cadence_sec=cadence,
            spool_dir=spool_dir / mode,
            log_fd=log_fd,
            decoder_path=decoder_path,
            keep_wav=keep_wav,
        )

        self._stream = None
        self._stream_start_epoch: Optional[float] = None
        self._first_rtp_ts: Optional[int] = None
        self._total_delivered: int = 0

    def start(self) -> None:
        """Start the managed RTP stream and slot worker."""
        from ka9q import ManagedStream

        self._stream = ManagedStream(
            control=self._control,
            frequency_hz=float(self._frequency_hz),
            preset=self._preset,
            sample_rate=self._sample_rate,
            encoding=self._encoding,
            on_samples=self._on_samples,
            on_stream_dropped=self._on_stream_dropped,
            on_stream_restored=self._on_stream_restored,
        )
        self._stream.start()
        self._slot_worker.start()
        logger.info(
            "%s %d Hz: managed stream started (sr=%d)",
            self._mode.upper(), self._frequency_hz, self._sample_rate,
        )

    def stop(self) -> None:
        """Stop the managed stream and slot worker."""
        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                logger.exception("Error stopping stream")
        self._slot_worker.stop()
        logger.info(
            "%s %d Hz: stream stopped (total_delivered=%d)",
            self._mode.upper(), self._frequency_hz, self._total_delivered,
        )

    def _on_samples(self, samples: np.ndarray, quality) -> None:
        """ManagedStream callback — push samples into the ring."""
        n = len(samples)
        if n == 0:
            return

        now = time.time()

        if self._stream_start_epoch is None:
            self._stream_start_epoch = now - (
                quality.total_samples_delivered / self._sample_rate
            )
            self._first_rtp_ts = quality.first_rtp_timestamp
            logger.info(
                "%s %d Hz: first samples received (n=%d)",
                self._mode.upper(), self._frequency_hz, n,
            )

        batch_start_sample = quality.total_samples_delivered - n
        utc_of_first = self._stream_start_epoch + (
            batch_start_sample / self._sample_rate
        )

        self._ring.push(samples, utc_of_first)
        self._total_delivered = quality.total_samples_delivered

    def _on_stream_dropped(self, reason: str) -> None:
        logger.warning(
            "%s %d Hz: stream dropped — %s",
            self._mode.upper(), self._frequency_hz, reason,
        )
        self._stream_start_epoch = None

    def _on_stream_restored(self, channel_info) -> None:
        logger.info(
            "%s %d Hz: stream restored",
            self._mode.upper(), self._frequency_hz,
        )

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def frequency_hz(self) -> int:
        return self._frequency_hz
