"""Minimal WAV writer for decode_ft8 input.

Writes standard RIFF WAV: mono, 16-bit signed PCM, little-endian.
This matches what jt-decoded/pcmrecord produces for decode_ft8.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


def write_wav(
    path: Path,
    samples: np.ndarray,
    sample_rate: int,
    frequency_hz: int = 0,
) -> None:
    """Write float32 samples as a 16-bit PCM WAV file.

    Args:
        path: Output WAV file path.
        samples: float32 audio samples, normalized [-1, 1].
        sample_rate: Sample rate in Hz (e.g. 12000).
        frequency_hz: Optional center frequency for xattr metadata.
    """
    int16_samples = _float32_to_int16(samples)
    data_bytes = int16_samples.tobytes()
    data_size = len(data_bytes)

    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8

    header = struct.pack(
        "<4sI4s"     # RIFF header
        "4sIHHIIHH"  # fmt chunk
        "4sI",       # data chunk header
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,                # PCM format
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(header)
        f.write(data_bytes)

    if frequency_hz:
        _set_xattrs(path, sample_rate, frequency_hz)


# RMS-target normalization. Per-slot peak-norm (789064f, reverted in
# 50bd7d9) failed because one transient sets the peak and scales every
# other sample into the noise floor. RMS is dominated by bulk signal,
# not spikes, so one impulse barely moves it. MAX_GAIN ceilings silent
# slots so pure noise doesn't get amplified into saturating garbage.
_TARGET_RMS_INT16 = 2000.0   # ~ -24 dBFS, leaves ~24 dB peak headroom
_MAX_GAIN = 2000.0


def _float32_to_int16(samples: np.ndarray) -> np.ndarray:
    """Convert float32 audio to int16 with RMS-target normalization."""
    if samples.size == 0:
        return np.zeros(0, dtype=np.int16)
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    if rms > 0.0:
        gain = min(_TARGET_RMS_INT16 / (rms * 32767.0), _MAX_GAIN)
    else:
        gain = 1.0
    scaled = samples * gain * 32767.0
    return np.clip(scaled, -32768.0, 32767.0).astype(np.int16)


def _set_xattrs(path: Path, sample_rate: int, frequency_hz: int) -> None:
    """Set xattrs matching pcmrecord/jt-decoded conventions.

    These are optional — decode_ft8 works without them — but they
    let downstream tools identify the source without parsing filenames.
    """
    try:
        import os
        os.setxattr(str(path), "user.frequency", str(frequency_hz).encode())
    except (OSError, AttributeError):
        pass
