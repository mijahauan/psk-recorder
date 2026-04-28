"""Interactive `config init` and `config edit` for psk-recorder.

Implements CONTRACT-v0.5 §14: sigmond invokes these via
`smd config init|edit psk-recorder [<instance>]`, passing
`STATION_CALL`, `STATION_GRID`, `SIGMOND_INSTANCE`, and
`SIGMOND_RADIOD_STATUS` as advisory defaults.  The script honors them
as prompt defaults, never as overrides.

Standalone usage works too (env vars unset → empty defaults).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

from .config import DEFAULT_CONFIG_PATH


# Repo-relative location of the template (works for editable installs and
# packaged installs alike).
def _find_template() -> Optional[Path]:
    candidates = [
        Path(__file__).resolve().parent.parent.parent
            / "config" / "psk-recorder-config.toml.template",
        Path("/opt/git/psk-recorder/config/psk-recorder-config.toml.template"),
        Path("/usr/local/share/psk-recorder/psk-recorder-config.toml.template"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# Public entry points (called from cli.py)
# ---------------------------------------------------------------------------

def cmd_config_init(args) -> int:
    target = _resolve_target(args)
    if target.exists() and not getattr(args, "reconfig", False):
        _err(f"{target} already exists.  Pass --reconfig to overwrite, or "
             f"run `psk-recorder config edit` instead.")
        return 1

    template = _find_template()
    if template is None:
        _err("psk-recorder template not found; reinstall or pass --template")
        return 1

    # Read template, then patch with operator/env values.
    body = template.read_text()
    values = _collect_init_values(args)
    body = _apply_init_substitutions(body, values)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    _ok(f"wrote {target}")
    _info(f"reporter: {values['callsign']}    grid: {values['grid']}")
    _info(f"radiod:   id={values['radiod_id']}  status={values['radiod_status']}")
    _info("")
    _info("Next steps:")
    _info(f"  1. Review the FT8/FT4 freq_hz arrays in {target}")
    _info(f"  2. Validate: psk-recorder validate --json")
    _info(f"  3. Start:    sudo systemctl enable --now "
          f"psk-recorder@{values['radiod_id']}.service")
    return 0


def cmd_config_edit(args) -> int:
    target = _resolve_target(args)
    if not target.exists():
        _err(f"{target} does not exist.  Run `psk-recorder config init` first.")
        return 1

    try:
        with open(target, "rb") as f:
            current = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        _err(f"failed to read {target}: {e}")
        return 1

    # In edit mode, the *current config* is the primary default; the env bag
    # only fills in values where the config is empty (CONTRACT-v0.5 §14, edit
    # flow).
    cur_call = (current.get("station") or {}).get("callsign", "")
    cur_grid = (current.get("station") or {}).get("grid_square", "")
    blocks = _radiod_blocks(current)
    block, block_index = _select_radiod_block(blocks, args)
    if block is None:
        return 1
    cur_id     = block.get("id", "")
    cur_status = block.get("radiod_status", "")

    if getattr(args, "non_interactive", False):
        # Display only.
        _info(f"station.callsign      = {cur_call}")
        _info(f"station.grid_square   = {cur_grid}")
        _info(f"radiod[{block_index}].id           = {cur_id}")
        _info(f"radiod[{block_index}].radiod_status = {cur_status}")
        return 0

    new_call = _prompt(
        "Callsign",
        cur_call or _default_reporter_callsign(
            os.environ.get("STATION_CALL", "")))
    new_grid = _prompt("Grid square",
                       cur_grid or os.environ.get("STATION_GRID", ""))
    new_id = _prompt("Radiod id",
                     cur_id or os.environ.get("SIGMOND_INSTANCE", ""))
    new_status = _prompt("Radiod status DNS",
                         cur_status or
                         os.environ.get("SIGMOND_RADIOD_STATUS", ""))

    body = target.read_text()
    body = _replace_station_field(body, "callsign",    new_call)
    body = _replace_station_field(body, "grid_square", new_grid)
    body = _replace_radiod_field(body, block_index, "id",            new_id)
    body = _replace_radiod_field(body, block_index, "radiod_status", new_status)

    if body == target.read_text():
        _info("no changes")
        return 0

    target.write_text(body)
    _ok(f"updated {target}")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_target(args) -> Path:
    return Path(getattr(args, "config", None) or DEFAULT_CONFIG_PATH)


def _collect_init_values(args) -> dict:
    """Build the substitution dict for init.  Env vars are defaults; an
    interactive prompt fills in anything missing unless --non-interactive."""
    call = os.environ.get("STATION_CALL", "")
    grid = os.environ.get("STATION_GRID", "")
    instance = os.environ.get("SIGMOND_INSTANCE", "")
    status = os.environ.get("SIGMOND_RADIOD_STATUS", "")
    default_callsign = _default_reporter_callsign(call)

    if getattr(args, "non_interactive", False):
        return {
            "callsign":      default_callsign or "YOURCALL",
            "grid":          grid or "AA00aa",
            "radiod_id":     instance or "my-rx888",
            "radiod_status": status or (
                f"{instance}-status.local" if instance else "my-rx888-status.local"
            ),
        }

    callsign = _prompt("Callsign", default_callsign, required=True)
    grid_square = _prompt("Grid square", grid, required=True)
    radiod_id = _prompt("Radiod id (e.g. bee1-rx888)",
                        instance, required=True)
    default_status = status or f"{radiod_id}-status.local"
    radiod_status = _prompt("Radiod status DNS", default_status, required=True)
    return {
        "callsign":      callsign,
        "grid":          grid_square,
        "radiod_id":     radiod_id,
        "radiod_status": radiod_status,
    }


def _default_reporter_callsign(call: str) -> str:
    """Compose the reporter callsign default from the bare callsign per
    CONTRACT-v0.5 §14.6:

    - single radiod (SIGMOND_RADIOD_COUNT == 1 or unset): bare callsign.
    - multi-radiod: AC0G/B<n> where n is SIGMOND_RADIOD_INDEX, falling
      back to 1 when not set.

    Returns "" when no callsign is known.
    """
    if not call:
        return ""
    try:
        count = int(os.environ.get("SIGMOND_RADIOD_COUNT", "1") or "1")
    except ValueError:
        count = 1
    if count <= 1:
        return call
    try:
        index = int(os.environ.get("SIGMOND_RADIOD_INDEX", "1") or "1")
    except ValueError:
        index = 1
    return f"{call}/B{index}"


def _apply_init_substitutions(body: str, values: dict) -> str:
    body = _replace_station_field(body, "callsign",    values["callsign"])
    body = _replace_station_field(body, "grid_square", values["grid"])
    body = _replace_radiod_field(body, 0, "id",            values["radiod_id"])
    body = _replace_radiod_field(body, 0, "radiod_status", values["radiod_status"])
    return body


def _radiod_blocks(config: dict) -> list[dict]:
    blocks = config.get("radiod", [])
    if isinstance(blocks, dict):
        blocks = [blocks]
    return list(blocks)


def _select_radiod_block(blocks: list[dict], args) -> tuple:
    """Return (block, index) of the radiod block to edit.
    Picks: SIGMOND_INSTANCE if set, else the only block, else prompts."""
    if not blocks:
        _err("config has no [[radiod]] blocks")
        return None, -1

    target_id = os.environ.get("SIGMOND_INSTANCE", "") or \
                getattr(args, "radiod_id", None)

    if target_id:
        for i, b in enumerate(blocks):
            if b.get("id") == target_id:
                return b, i
        _err(f"no [[radiod]] block with id={target_id!r}; "
             f"available: {', '.join(b.get('id', '?') for b in blocks)}")
        return None, -1

    if len(blocks) == 1:
        return blocks[0], 0

    if getattr(args, "non_interactive", False):
        _err(f"multiple [[radiod]] blocks; specify with --radiod-id or "
             f"SIGMOND_INSTANCE")
        return None, -1

    print("\nMultiple [[radiod]] blocks present.  Pick one:")
    for i, b in enumerate(blocks, start=1):
        print(f"  {i}) id={b.get('id', '?')}  status={b.get('radiod_status', '?')}")
    while True:
        choice = input("Select [1-{}]: ".format(len(blocks))).strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(blocks):
                return blocks[idx], idx
        except ValueError:
            pass
        print("  invalid choice")


# ---------------------------------------------------------------------------
# Field substitution — line-oriented; preserves comments and surrounding TOML.
# ---------------------------------------------------------------------------

_STATION_PAT = re.compile(
    r'^(\s*{key}\s*=\s*)"[^"]*"(.*)$'
)


def _replace_station_field(body: str, key: str, value: str) -> str:
    """Replace `key = "..."` inside the [station] block."""
    pat = re.compile(
        r'^(\s*' + re.escape(key) + r'\s*=\s*)"[^"]*"(.*)$', re.MULTILINE
    )
    in_station = False
    out_lines: list[str] = []
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            in_station = (stripped == "[station]")
        if in_station:
            line = pat.sub(rf'\g<1>"{value}"\g<2>', line)
        out_lines.append(line)
    return ''.join(out_lines)


def _replace_radiod_field(body: str, index: int, key: str, value: str) -> str:
    """Replace `key = "..."` inside the Nth top-level [[radiod]] block.
    Stops at the next [[radiod]] header or the next top-level table that
    isn't a sub-table of radiod."""
    pat = re.compile(
        r'^(\s*' + re.escape(key) + r'\s*=\s*)"[^"]*"(.*)$', re.MULTILINE
    )
    out_lines: list[str] = []
    radiod_count = -1
    in_target = False
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == "[[radiod]]":
            radiod_count += 1
            in_target = (radiod_count == index)
        elif (stripped.startswith('[[') and stripped.endswith(']]')
              and stripped != "[[radiod]]"):
            in_target = False
        elif (stripped.startswith('[') and not stripped.startswith('[[')
              and not stripped.startswith('[radiod.')):
            # A top-level [section] that isn't [radiod.<sub>] ends our scope.
            in_target = False
        if in_target:
            line = pat.sub(rf'\g<1>"{value}"\g<2>', line)
        out_lines.append(line)
    return ''.join(out_lines)


# ---------------------------------------------------------------------------
# Prompts (small, dependency-free)
# ---------------------------------------------------------------------------

def _prompt(label: str, default: str, *, required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"  {label}{suffix}: ").strip()
        except EOFError:
            raw = ""
        result = raw or default
        if result or not required:
            return result
        print("  This field is required.")


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _ok(msg: str) -> None:
    print(f"\033[32m✓\033[0m {msg}")


def _info(msg: str) -> None:
    print(f"  {msg}")


def _err(msg: str) -> None:
    print(f"\033[31m✗\033[0m {msg}", file=sys.stderr)
