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


def _float32_to_int16(samples: np.ndarray) -> np.ndarray:
    """Convert float32 [-1, 1] to int16 [-32767, 32767]."""
    # NOTE: tried per-slot peak normalization (commit 789064f). It dropped
    # decode rate ~20× on B3-1 because a transient (QRM burst, lightning,
    # carrier sweep) sets the slot peak and every other sample gets scaled
    # into the noise floor. decode_ft8 is amplitude-sensitive; trust the
    # radiod output level and just clip. If a future change is needed,
    # robust-peak (e.g. 98th percentile) or RMS-based would be safer.
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767).astype(np.int16)


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
