"""TOML config loader and defaults for psk-recorder."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG_PATH = Path("/etc/psk-recorder/psk-recorder-config.toml")

DEFAULTS: dict[str, Any] = {
    "paths": {
        "spool_dir": "/var/lib/psk-recorder",
        "log_dir": "/var/log/psk-recorder",
        "decoder": "/usr/local/bin/decode_ft8",
        "pskreporter": "/usr/local/bin/pskreporter",
        "keep_wav": False,
    },
}

FT8_CADENCE_SEC = 15.0
FT4_CADENCE_SEC = 7.5
DEFAULT_SAMPLE_RATE = 12000
DEFAULT_PRESET = "usb"
DEFAULT_ENCODING = "s16be"


def load_config(path: Path | None = None) -> dict:
    """Load and merge config with defaults."""
    config_path = path or Path(
        os.environ.get("PSK_RECORDER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    raw.setdefault("paths", {})
    for key, val in DEFAULTS["paths"].items():
        raw["paths"].setdefault(key, val)

    return raw


def resolve_radiod_block(config: dict, radiod_id: str | None) -> dict:
    """Find the [[radiod]] block matching radiod_id.

    If radiod_id is None, the config must contain exactly one [[radiod]].
    """
    radiod_blocks = config.get("radiod", [])
    if isinstance(radiod_blocks, dict):
        radiod_blocks = [radiod_blocks]

    if not radiod_blocks:
        raise ValueError("Config contains no [[radiod]] blocks")

    if radiod_id is None:
        if len(radiod_blocks) != 1:
            raise ValueError(
                f"--radiod-id required: config has {len(radiod_blocks)} "
                f"[[radiod]] blocks"
            )
        return radiod_blocks[0]

    for block in radiod_blocks:
        if block.get("id") == radiod_id:
            return block

    available = [b.get("id", "<unnamed>") for b in radiod_blocks]
    raise ValueError(
        f"No [[radiod]] block with id={radiod_id!r}. "
        f"Available: {', '.join(available)}"
    )


def get_freqs(radiod_block: dict, mode: str) -> list[int]:
    """Extract frequency list for a mode ('ft4' or 'ft8')."""
    mode_block = radiod_block.get(mode, {})
    return list(mode_block.get("freqs_hz", []))


def get_mode_params(radiod_block: dict, mode: str) -> dict:
    """Extract sample_rate, preset, encoding for a mode."""
    mode_block = radiod_block.get(mode, {})
    return {
        "sample_rate": int(mode_block.get("sample_rate", DEFAULT_SAMPLE_RATE)),
        "preset": mode_block.get("preset", DEFAULT_PRESET),
        "encoding": mode_block.get("encoding", DEFAULT_ENCODING),
    }


def resolve_radiod_status(radiod_block: dict) -> str:
    """Resolve the radiod mDNS hostname.

    Precedence:
      1. RADIOD_<ID>_STATUS from environment (sigmond-supplied)
      2. radiod_status field in the [[radiod]] block (standalone fallback)
    """
    radiod_id = radiod_block.get("id", "")
    env_key = f"RADIOD_{radiod_id.upper().replace('-', '_')}_STATUS"
    from_env = os.environ.get(env_key)
    if from_env:
        return from_env

    status = radiod_block.get("radiod_status")
    if not status:
        raise ValueError(
            f"[[radiod]] id={radiod_id!r} has no radiod_status and "
            f"{env_key} is not set in the environment"
        )
    return status
