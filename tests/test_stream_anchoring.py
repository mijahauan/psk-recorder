"""Tests for ChannelSink UTC anchoring via AuthorityReader
(hf-timestd/docs/METROLOGY.md §4.5.1 RTP-reference invariant)."""

import logging
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from unittest import mock

import numpy as np

from psk_recorder.core.stream import (
    AUTHORITY_REANCHOR_THRESHOLD_SEC,
    AUTHORITY_REFRESH_EVERY_SAMPLES,
    ChannelSink,
)


@dataclass
class _FakeQuality:
    total_samples_delivered: int = 0


class _FakeSnap:
    """Stand-in for AuthoritySnapshot exposing just what the sink reads."""
    def __init__(self, usable: bool, offset_sec: float = 0.0):
        self._usable = usable
        self._offset_sec = offset_sec

    @property
    def offset_usable(self) -> bool:
        return self._usable

    @property
    def offset_seconds(self) -> float:
        return self._offset_sec


class _FakeReader:
    def __init__(self, snaps: List[Optional[_FakeSnap]]):
        """snaps is a queue of successive return values for read()."""
        self._snaps = list(snaps)

    def read(self):
        if not self._snaps:
            return None
        # Last value is sticky — repeated reads after queue drains
        # return the final snap.
        if len(self._snaps) > 1:
            return self._snaps.pop(0)
        return self._snaps[0]


def _make_sink(reader=None) -> ChannelSink:
    tmp = Path(tempfile.mkdtemp())
    log_fd = open(tmp / "log", "ab")
    sink = ChannelSink(
        mode="ft8",
        frequency_hz=14_074_000,
        sample_rate=12_000,
        preset="usb",
        encoding=0,
        spool_dir=tmp,
        log_fd=log_fd,
        decoder_path="/nonexistent",
        keep_wav=False,
        authority_reader=reader,
    )
    # Attach tmp for cleanup by caller.
    sink._tmp_dir = tmp  # type: ignore[attr-defined]
    return sink


def _cleanup_sink(sink: ChannelSink) -> None:
    tmp = getattr(sink, "_tmp_dir", None)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


class TestChannelSinkAuthorityAnchoring(unittest.TestCase):
    def test_first_call_with_authority_uses_offset_for_anchor(self):
        """Authority available at stream start: offset is applied to the
        wall-clock anchor so RTP time mapped through the anchor matches
        UTC + offset."""
        reader = _FakeReader([_FakeSnap(usable=True, offset_sec=107.0)])
        sink = _make_sink(reader=reader)
        try:
            samples = np.zeros(120, dtype=np.float32)
            quality = _FakeQuality(total_samples_delivered=120)
            with mock.patch("psk_recorder.core.stream.time.time",
                            return_value=1000.0):
                sink.on_samples(samples, quality)
            # Expected epoch: (1000 + 107) - (120/12000) = 1106.990
            self.assertAlmostEqual(sink._stream_start_epoch, 1106.990, places=3)
            self.assertEqual(sink.anchor_source, "authority")
            self.assertAlmostEqual(sink.anchor_offset_sec, 107.0, places=3)
        finally:
            _cleanup_sink(sink)

    def test_first_call_without_authority_falls_back_to_wall_clock(self):
        """No authority: anchor uses wall_clock directly, matching legacy
        behavior."""
        reader = _FakeReader([None])
        sink = _make_sink(reader=reader)
        try:
            samples = np.zeros(120, dtype=np.float32)
            quality = _FakeQuality(total_samples_delivered=120)
            with self.assertLogs("psk_recorder.core.stream", level="WARNING") as cm, \
                 mock.patch("psk_recorder.core.stream.time.time", return_value=1000.0):
                sink.on_samples(samples, quality)
            # Expected epoch: 1000 - (120/12000) = 999.990 (no offset)
            self.assertAlmostEqual(sink._stream_start_epoch, 999.990, places=3)
            self.assertEqual(sink.anchor_source, "wall_clock")
            self.assertIsNone(sink.anchor_offset_sec)
            self.assertTrue(any("standalone mode" in r.message for r in cm.records))
        finally:
            _cleanup_sink(sink)

    def test_unusable_snapshot_treated_as_missing(self):
        """t_level_active=None -> offset_usable=False -> wall_clock path."""
        reader = _FakeReader([_FakeSnap(usable=False)])
        sink = _make_sink(reader=reader)
        try:
            samples = np.zeros(120, dtype=np.float32)
            quality = _FakeQuality(total_samples_delivered=120)
            sink.on_samples(samples, quality)
            self.assertEqual(sink.anchor_source, "wall_clock")
        finally:
            _cleanup_sink(sink)

    def test_reader_exception_treated_as_unavailable(self):
        class _BoomReader:
            def read(self):
                raise RuntimeError("boom")
        sink = _make_sink(reader=_BoomReader())
        try:
            samples = np.zeros(120, dtype=np.float32)
            quality = _FakeQuality(total_samples_delivered=120)
            sink.on_samples(samples, quality)
            self.assertEqual(sink.anchor_source, "wall_clock")
        finally:
            _cleanup_sink(sink)

    def test_refresh_absorbs_large_offset_drift(self):
        """Authority offset changes by > threshold after the refresh
        window; the stream epoch rebases by the drift amount."""
        # First snap: 0 ms offset. Later: +200 ms (beyond 50 ms threshold).
        reader = _FakeReader([
            _FakeSnap(usable=True, offset_sec=0.0),
            _FakeSnap(usable=True, offset_sec=0.200),
        ])
        sink = _make_sink(reader=reader)
        try:
            sr = 12_000
            samples_chunk = np.zeros(sr, dtype=np.float32)  # 1 s each
            delivered = 0

            # First chunk anchors the stream at offset=0.
            delivered += sr
            q = _FakeQuality(total_samples_delivered=delivered)
            with mock.patch("psk_recorder.core.stream.time.time", return_value=2000.0):
                sink.on_samples(samples_chunk, q)
            epoch_initial = sink._stream_start_epoch

            # Drive enough samples through to trigger the refresh.
            total_to_feed = AUTHORITY_REFRESH_EVERY_SAMPLES + sr
            while delivered < total_to_feed:
                delivered += sr
                q = _FakeQuality(total_samples_delivered=delivered)
                with mock.patch("psk_recorder.core.stream.time.time",
                                return_value=2000.0 + delivered / sr):
                    sink.on_samples(samples_chunk, q)

            # Epoch should have been rebased by ~0.200 s forward.
            self.assertAlmostEqual(
                sink._stream_start_epoch - epoch_initial, 0.200, places=3,
            )
            self.assertAlmostEqual(sink.anchor_offset_sec, 0.200, places=3)
        finally:
            _cleanup_sink(sink)

    def test_refresh_ignores_small_drift_below_threshold(self):
        """Offset drifts by less than AUTHORITY_REANCHOR_THRESHOLD_SEC;
        no rebase should occur (noise not worth the log)."""
        reader = _FakeReader([
            _FakeSnap(usable=True, offset_sec=0.0),
            _FakeSnap(usable=True, offset_sec=0.010),  # 10 ms, well under 50 ms
        ])
        sink = _make_sink(reader=reader)
        try:
            sr = 12_000
            samples_chunk = np.zeros(sr, dtype=np.float32)
            delivered = 0

            delivered += sr
            q = _FakeQuality(total_samples_delivered=delivered)
            with mock.patch("psk_recorder.core.stream.time.time", return_value=3000.0):
                sink.on_samples(samples_chunk, q)
            epoch_initial = sink._stream_start_epoch

            total_to_feed = AUTHORITY_REFRESH_EVERY_SAMPLES + sr
            while delivered < total_to_feed:
                delivered += sr
                q = _FakeQuality(total_samples_delivered=delivered)
                sink.on_samples(samples_chunk, q)

            # Epoch is unchanged (drift 10 ms < threshold 50 ms).
            self.assertEqual(sink._stream_start_epoch, epoch_initial)
            self.assertAlmostEqual(sink.anchor_offset_sec, 0.0, places=3)
        finally:
            _cleanup_sink(sink)

    def test_authority_becomes_available_mid_run(self):
        """Initially anchored via wall_clock; authority shows up later —
        epoch should rebase by the full offset on the next refresh."""
        reader = _FakeReader([
            None,                                 # at anchor time
            _FakeSnap(usable=True, offset_sec=0.500),  # later
        ])
        sink = _make_sink(reader=reader)
        try:
            sr = 12_000
            samples_chunk = np.zeros(sr, dtype=np.float32)
            delivered = 0

            delivered += sr
            q = _FakeQuality(total_samples_delivered=delivered)
            with mock.patch("psk_recorder.core.stream.time.time", return_value=4000.0):
                sink.on_samples(samples_chunk, q)
            self.assertEqual(sink.anchor_source, "wall_clock")
            epoch_initial = sink._stream_start_epoch

            total_to_feed = AUTHORITY_REFRESH_EVERY_SAMPLES + sr
            while delivered < total_to_feed:
                delivered += sr
                q = _FakeQuality(total_samples_delivered=delivered)
                sink.on_samples(samples_chunk, q)

            self.assertEqual(sink.anchor_source, "authority")
            self.assertAlmostEqual(
                sink._stream_start_epoch - epoch_initial, 0.500, places=3,
            )
        finally:
            _cleanup_sink(sink)

    def test_empty_batch_does_nothing(self):
        reader = _FakeReader([_FakeSnap(usable=True, offset_sec=0.0)])
        sink = _make_sink(reader=reader)
        try:
            q = _FakeQuality(total_samples_delivered=0)
            sink.on_samples(np.array([], dtype=np.float32), q)
            self.assertIsNone(sink._stream_start_epoch)
            self.assertIsNone(sink.anchor_source)
        finally:
            _cleanup_sink(sink)


if __name__ == "__main__":
    unittest.main()
