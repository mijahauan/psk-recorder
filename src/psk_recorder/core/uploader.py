"""PskReporterUploader: supervises long-running pskreporter subprocesses.

One pskreporter process per (radiod, mode) pair, tailing the spot log.
Restarts on exit with exponential backoff.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MAX_BACKOFF = 60.0
INITIAL_BACKOFF = 2.0


class PskReporterUploader:
    """Manages a pskreporter subprocess that tails a spot log."""

    def __init__(
        self,
        pskreporter_path: str,
        log_path: Path,
        callsign: str,
        grid_square: str,
    ):
        self._binary = pskreporter_path
        self._log_path = log_path
        self._callsign = callsign
        self._grid_square = grid_square
        self._proc: Optional[subprocess.Popen] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._backoff = INITIAL_BACKOFF

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._supervise_loop, daemon=True,
            name=f"uploader-{self._log_path.stem}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _supervise_loop(self) -> None:
        while self._running:
            if not Path(self._binary).is_file():
                logger.warning(
                    "pskreporter binary not found at %s — "
                    "spots will accumulate in %s but not upload",
                    self._binary, self._log_path,
                )
                while self._running:
                    time.sleep(10.0)
                return

            if not self._log_path.exists():
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_path.touch()

            try:
                self._start_process()
                self._backoff = INITIAL_BACKOFF
                while self._running:
                    ret = self._proc.poll()
                    if ret is not None:
                        logger.warning(
                            "pskreporter exited with code %d, restarting "
                            "in %.0f s", ret, self._backoff,
                        )
                        break
                    time.sleep(1.0)
            except OSError as exc:
                logger.error("Failed to start pskreporter: %s", exc)

            if self._running:
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    def _start_process(self) -> None:
        cmd = [
            self._binary,
            "-c", self._callsign,
            "-g", self._grid_square,
            str(self._log_path),
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        logger.info(
            "pskreporter started (pid=%d) tailing %s",
            self._proc.pid, self._log_path,
        )
