"""PskRecorder: orchestrates one radiod's FT4/FT8 channels.

One PskRecorder per radiod instance (= one systemd unit).
Creates ChannelStream objects for each frequency, manages log
file descriptors, and supervises PskReporterUploaders.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path
from typing import Optional

from psk_recorder.config import (
    FT4_CADENCE_SEC,
    FT8_CADENCE_SEC,
    get_freqs,
    get_mode_params,
    resolve_radiod_status,
)
from psk_recorder.core.stream import ChannelStream
from psk_recorder.core.uploader import PskReporterUploader

logger = logging.getLogger(__name__)


class PskRecorder:
    """Manages all FT4/FT8 channels for a single radiod."""

    def __init__(self, config: dict, radiod_block: dict):
        self._config = config
        self._radiod = radiod_block
        self._radiod_id = radiod_block.get("id", "default")
        self._paths = config.get("paths", {})
        self._station = config.get("station", {})

        self._streams: list[ChannelStream] = []
        self._uploaders: list[PskReporterUploader] = []
        self._log_fds: dict[str, object] = {}
        self._running = False

    def run(self) -> None:
        """Main entry: provision channels, start streams, block until signal."""
        self._running = True
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        try:
            self._provision_channels()
            self._start_streams()
            self._start_uploaders()
            self._notify_ready()
            self._main_loop()
        except Exception:
            logger.exception("Fatal error in recorder")
        finally:
            self._shutdown()

    def _provision_channels(self) -> None:
        """Create ChannelStream objects for all configured frequencies."""
        from ka9q import RadiodControl

        status = resolve_radiod_status(self._radiod)
        logger.info("Connecting to radiod at %s", status)
        self._control = RadiodControl(status)

        spool_root = Path(self._paths.get(
            "spool_dir", "/var/lib/psk-recorder"
        )) / self._radiod_id
        log_dir = Path(self._paths.get(
            "log_dir", "/var/log/psk-recorder"
        ))
        decoder = self._paths.get("decoder", "/usr/local/bin/decode_ft8")
        keep_wav = self._paths.get("keep_wav", False)

        for mode in ("ft8", "ft4"):
            freqs = get_freqs(self._radiod, mode)
            if not freqs:
                continue

            params = get_mode_params(self._radiod, mode)
            sample_rate = params["sample_rate"]
            preset = params["preset"]
            encoding_str = params.get("encoding", "s16be")
            encoding_int = _resolve_encoding(encoding_str)

            log_path = log_dir / f"{self._radiod_id}-{mode}.log"
            if mode not in self._log_fds:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_fds[mode] = open(log_path, "ab")

            for freq_hz in freqs:
                logger.info(
                    "Provisioning %s %d Hz (sr=%d, preset=%s, enc=%s)",
                    mode.upper(), freq_hz, sample_rate, preset, encoding_str,
                )
                stream = ChannelStream(
                    control=self._control,
                    mode=mode,
                    frequency_hz=freq_hz,
                    sample_rate=sample_rate,
                    preset=preset,
                    encoding=encoding_int,
                    radiod_id=self._radiod_id,
                    spool_dir=spool_root,
                    log_fd=self._log_fds[mode],
                    decoder_path=decoder,
                    keep_wav=keep_wav,
                )
                self._streams.append(stream)

        logger.info(
            "Provisioned %d channels on radiod %s",
            len(self._streams), self._radiod_id,
        )

    def _start_streams(self) -> None:
        for stream in self._streams:
            try:
                stream.start()
            except Exception:
                logger.exception(
                    "Failed to start %s %d Hz",
                    stream.mode, stream.frequency_hz,
                )

    def _start_uploaders(self) -> None:
        pskreporter = self._paths.get(
            "pskreporter", "/usr/local/bin/pskreporter-sender"
        )
        callsign = self._station.get("callsign", "")
        grid = self._station.get("grid_square", "")

        if not callsign:
            logger.warning("No callsign configured — pskreporter will not start")
            return

        log_dir = Path(self._paths.get("log_dir", "/var/log/psk-recorder"))

        for mode in ("ft8", "ft4"):
            if not get_freqs(self._radiod, mode):
                continue
            log_path = log_dir / f"{self._radiod_id}-{mode}.log"
            uploader = PskReporterUploader(
                pskreporter_path=pskreporter,
                log_path=log_path,
                callsign=callsign,
                grid_square=grid,
                mode=mode,
            )
            uploader.start()
            self._uploaders.append(uploader)

    def _notify_ready(self) -> None:
        """Send sd_notify READY=1 if running under systemd."""
        try:
            addr = os.environ.get("NOTIFY_SOCKET")
            if addr:
                import socket
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    if addr.startswith("@"):
                        addr = "\0" + addr[1:]
                    sock.connect(addr)
                    sock.sendall(b"READY=1")
                finally:
                    sock.close()
                logger.info("sd_notify READY=1 sent")
        except Exception:
            logger.debug("sd_notify failed (not running under systemd?)")

    def _main_loop(self) -> None:
        """Block until signalled, petting the watchdog periodically."""
        watchdog_usec = os.environ.get("WATCHDOG_USEC")
        pet_interval = (
            int(watchdog_usec) / 1_000_000 / 2
            if watchdog_usec else 30.0
        )

        while self._running:
            time.sleep(min(pet_interval, 5.0))
            self._pet_watchdog()

    def _pet_watchdog(self) -> None:
        try:
            addr = os.environ.get("NOTIFY_SOCKET")
            if addr:
                import socket
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    if addr.startswith("@"):
                        addr = "\0" + addr[1:]
                    sock.connect(addr)
                    sock.sendall(b"WATCHDOG=1")
                finally:
                    sock.close()
        except Exception:
            pass

    def _on_signal(self, signum, frame) -> None:
        logger.info("Received signal %d, shutting down", signum)
        self._running = False

    def _shutdown(self) -> None:
        logger.info("Shutting down...")
        for uploader in self._uploaders:
            uploader.stop()
        for stream in self._streams:
            stream.stop()
        for fd in self._log_fds.values():
            try:
                fd.close()
            except Exception:
                pass
        if hasattr(self, "_control"):
            try:
                self._control.close()
            except Exception:
                pass
        logger.info("Shutdown complete")


def _resolve_encoding(enc_str: str) -> int:
    """Map config encoding string to ka9q.Encoding integer."""
    mapping = {
        "s16be": 2,
        "s16le": 1,
        "f32": 4,
        "f32le": 4,
        "f32be": 8,
    }
    return mapping.get(enc_str.lower(), 2)
