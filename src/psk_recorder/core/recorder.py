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
from psk_recorder.core.stream import ChannelSink
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

        self._sinks: list[ChannelSink] = []
        self._multi_streams: list = []
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
        """Create ChannelSinks and register them with MultiStream(s).

        One MultiStream per unique multicast group, keyed on the
        (mcast_addr, port) returned by ensure_channel(). In the common
        case (FT8+FT4 share preset/sample_rate/encoding) all channels
        land on one group and we end up with a single MultiStream.
        """
        from ka9q import MultiStream, RadiodControl

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

        multi_by_group: dict[tuple, object] = {}

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
                sink = ChannelSink(
                    mode=mode,
                    frequency_hz=freq_hz,
                    sample_rate=sample_rate,
                    preset=preset,
                    encoding=encoding_int,
                    spool_dir=spool_root,
                    log_fd=self._log_fds[mode],
                    decoder_path=decoder,
                    keep_wav=keep_wav,
                )
                self._add_sink_to_multi(sink, multi_by_group)
                self._sinks.append(sink)

        self._multi_streams = list(multi_by_group.values())
        logger.info(
            "Provisioned %d channels across %d multicast group(s) on radiod %s",
            len(self._sinks), len(self._multi_streams), self._radiod_id,
        )

    def _add_sink_to_multi(
        self, sink: ChannelSink, multi_by_group: dict,
    ) -> None:
        """Attach sink to the MultiStream for its multicast group.

        Resolves the multicast group up-front via ensure_channel() so
        we can pick the right MultiStream (or create one) by
        (mcast_addr, port). MultiStream.add_channel() calls ensure_channel
        again internally — idempotent, one extra cheap status probe —
        but this keeps the grouping deterministic instead of relying on
        ValueError as control flow.
        """
        from ka9q import MultiStream

        info = self._control.ensure_channel(
            frequency_hz=float(sink.frequency_hz),
            preset=sink.preset,
            sample_rate=sink.sample_rate,
            encoding=sink.encoding,
        )
        key = (info.multicast_address, info.port)
        multi = multi_by_group.get(key)
        if multi is None:
            multi = MultiStream(control=self._control)
            multi_by_group[key] = multi

        multi.add_channel(
            frequency_hz=float(sink.frequency_hz),
            preset=sink.preset,
            sample_rate=sink.sample_rate,
            encoding=sink.encoding,
            on_samples=sink.on_samples,
            on_stream_dropped=sink.on_stream_dropped,
            on_stream_restored=sink.on_stream_restored,
        )

    def _start_streams(self) -> None:
        for sink in self._sinks:
            try:
                sink.start()
            except Exception:
                logger.exception(
                    "Failed to start sink %s %d Hz",
                    sink.mode, sink.frequency_hz,
                )
        for multi in self._multi_streams:
            try:
                multi.start()
            except Exception:
                logger.exception("Failed to start MultiStream")

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
        for multi in self._multi_streams:
            try:
                multi.stop()
            except Exception:
                logger.exception("Error stopping MultiStream")
        for sink in self._sinks:
            sink.stop()
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
