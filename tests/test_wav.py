"""Tests for the WAV writer."""

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from psk_recorder.core.wav import write_wav


class WavWriterTests(unittest.TestCase):

    def test_write_and_read_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.wav"
            samples = np.sin(
                np.linspace(0, 2 * np.pi * 440, 12000, dtype=np.float32)
            )
            write_wav(path, samples, sample_rate=12000)

            self.assertTrue(path.exists())
            data = path.read_bytes()

            self.assertEqual(data[:4], b"RIFF")
            self.assertEqual(data[8:12], b"WAVE")
            self.assertEqual(data[12:16], b"fmt ")

            fmt_size = struct.unpack_from("<I", data, 16)[0]
            self.assertEqual(fmt_size, 16)

            audio_format = struct.unpack_from("<H", data, 20)[0]
            self.assertEqual(audio_format, 1)

            channels = struct.unpack_from("<H", data, 22)[0]
            self.assertEqual(channels, 1)

            sr = struct.unpack_from("<I", data, 24)[0]
            self.assertEqual(sr, 12000)

            bits = struct.unpack_from("<H", data, 34)[0]
            self.assertEqual(bits, 16)

    def test_correct_sample_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.wav"
            n_samples = 180000
            samples = np.zeros(n_samples, dtype=np.float32)
            write_wav(path, samples, sample_rate=12000)

            data = path.read_bytes()
            data_size = struct.unpack_from("<I", data, 40)[0]
            self.assertEqual(data_size, n_samples * 2)

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sub" / "dir" / "test.wav"
            write_wav(path, np.zeros(100, dtype=np.float32), 12000)
            self.assertTrue(path.exists())

    def test_float32_to_int16_peak_normalized(self):
        # Writer normalizes to 0.95 full-scale. For peak=2.0, all samples
        # scale by 0.95/2.0 = 0.475 before int16 cast. 2.0 → 0.95 → 31128.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.wav"
            samples = np.array([2.0, -2.0, 0.5, -0.5], dtype=np.float32)
            write_wav(path, samples, sample_rate=12000)

            pcm = np.frombuffer(path.read_bytes()[44:], dtype=np.int16)
            # 0.95 * 32767 ≈ 31128.65 → astype(int16) truncates to 31128
            # for +peak but 31129 after sign on -peak (rounding asymmetry).
            self.assertIn(pcm[0], (31128, 31129))
            self.assertIn(pcm[1], (-31128, -31129))
            # Mid-amplitude samples scale by the same factor, not hard-clipped.
            self.assertAlmostEqual(pcm[2] / pcm[0], 0.25, places=2)

    def test_float32_quiet_signal_lifted_to_full_scale(self):
        # A signal peaking at 0.01 should still produce a near-full-scale WAV,
        # not a WAV that's 99% quantization noise.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.wav"
            samples = np.array([0.01, -0.01, 0.005], dtype=np.float32)
            write_wav(path, samples, sample_rate=12000)
            pcm = np.frombuffer(path.read_bytes()[44:], dtype=np.int16)
            self.assertGreater(abs(int(pcm[0])), 30000)

    def test_float32_all_zero_stays_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.wav"
            write_wav(path, np.zeros(16, dtype=np.float32), 12000)
            pcm = np.frombuffer(path.read_bytes()[44:], dtype=np.int16)
            self.assertTrue(np.all(pcm == 0))


if __name__ == "__main__":
    unittest.main()
