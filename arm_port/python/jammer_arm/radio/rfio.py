"""RFIO client: drive the optional C++ ``rfio`` daemon over localhost TCP.

Why this exists
---------------
UHD's Python bindings are the least reliable part of an ARM deployment: they need
Boost.Python + a matching numpy, and some distributions ship UHD without them.
When that happens the C++ daemon (``arm_port/cpp``) owns the UHD streams and this
device forwards RX/TX requests to it.  The backend loop, DSP, telemetry and safety
checks stay in Python.

Protocol (``arm_port/docs/04_rfio_protocol.md``)::

    control  : one JSON object per line
    rx       : request line  -> reply header line + nsamples*8 raw bytes (cf32 LE)
    tx       : request line + payload bytes -> reply line

Both directions are little-endian interleaved float32 (complex64), which is the
``fc32`` host format UHD is asked for.
"""

from __future__ import annotations

import json
import socket

import numpy as np

from ..config import JammerConfig
from .base import RadioDevice, RadioError

DEFAULT_RFIO_PORT = 5601


class RfioRadio(RadioDevice):
    def __init__(self, cfg: JammerConfig, host: str = "127.0.0.1",
                 port: int = DEFAULT_RFIO_PORT, connect_timeout: float = 5.0):
        super().__init__(cfg)
        self.info.backend = "rfio"
        self.host = host
        self.port = int(port)
        self.connect_timeout = float(connect_timeout)
        self._sock: socket.socket | None = None
        self._file = None

    # ── connection ───────────────────────────────────────────────────
    def open(self) -> None:
        self.close()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as exc:
            raise RadioError(
                f"cannot connect to rfio daemon at {self.host}:{self.port} ({exc}). "
                "Start it with `sudo jammer-rfio --port 5601` or build it from arm_port/cpp."
            ) from exc
        sock.settimeout(max(self.cfg.radio.recv_timeout_sec * 4.0, 2.0))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._file = sock.makefile("rb")

        r = self.cfg.radio
        reply = self._control({
            "cmd": "open",
            "device_args": r.device_args,
            "master_clock_rate": r.master_clock_rate_hz,
            "center_freq": r.center_freq_hz,
            "sample_rate": r.sample_rate_hz,
            "num_samples": r.num_samples,
            "rx_gain": r.rx_gain_db,
            "tx_gain": r.tx_gain_base_db,
            "rx_antenna": r.rx_antenna,
            "tx_antenna": r.tx_antenna,
            "clock_source": r.clock_source,
            "tdm": bool(r.tdm),
            "rx_mode": r.rx_mode,
            "settle_sec": r.settle_sec,
        })
        if reply.get("status") != "ok":
            self.close()
            raise RadioError(f"rfio open failed: {reply.get('error', reply)}")

        info = reply.get("info") or {}
        self.info.opened = True
        self.info.sample_rate_hz = float(info.get("rx_rate", r.sample_rate_hz))
        self.info.center_freq_hz = float(info.get("rx_freq", r.center_freq_hz))
        self.info.rx_gain_db = float(info.get("rx_gain", r.rx_gain_db))
        self.info.tx_gain_db = float(info.get("tx_gain", r.tx_gain_base_db))
        self.info.rx_antenna = str(info.get("rx_antenna", r.rx_antenna))
        self.info.tx_antenna = str(info.get("tx_antenna", r.tx_antenna))
        if info:
            self.info.details.update(info)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._control({"cmd": "close"}, expect_reply=False)
            except Exception:
                pass
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self.info.opened = False

    def rebuild_rx(self) -> None:
        reply = self._control({"cmd": "rebuild_rx"})
        if reply.get("status") != "ok":
            raise RadioError(f"rfio rebuild_rx failed: {reply.get('error', reply)}")

    def rebuild(self) -> None:
        reply = self._control({"cmd": "rebuild"})
        if reply.get("status") != "ok":
            raise RadioError(f"rfio rebuild failed: {reply.get('error', reply)}")

    # ── data path ────────────────────────────────────────────────────
    def _receive(self, num_samples: int) -> np.ndarray:
        self._send_line({"cmd": "rx", "nsamples": int(num_samples),
                         "timeout": float(self.cfg.radio.recv_timeout_sec)})
        header = self._read_line()
        if header.get("status") != "ok":
            code = header.get("error", "rx failed")
            if "overflow" in str(code):
                self.info.rx_overruns += 1
            raise RadioError(f"rfio rx: {code}")
        n = int(header.get("nsamples", 0))
        raw = self._read_exact(n * 8)
        return np.frombuffer(raw, dtype="<f4").view(np.complex64).copy()

    def _transmit(self, samples: np.ndarray, gain_db: float) -> None:
        payload = np.ascontiguousarray(samples, dtype=np.complex64)
        self._send_line({"cmd": "tx", "nsamples": int(payload.size),
                         "gain": float(gain_db),
                         "start_of_burst": True, "end_of_burst": True})
        if self._sock is None:
            raise RadioError("rfio daemon is not connected")
        try:
            self._sock.sendall(payload.astype("<c8").tobytes())
        except OSError as exc:
            self.close()
            raise RadioError(f"rfio tx send failed: {exc}") from exc
        reply = self._read_line()
        if reply.get("status") != "ok":
            raise RadioError(f"rfio tx: {reply.get('error', reply)}")

    # ── framing helpers ──────────────────────────────────────────────
    def _control(self, payload: dict, expect_reply: bool = True) -> dict:
        self._send_line(payload)
        return self._read_line() if expect_reply else {}

    def _send_line(self, payload: dict) -> None:
        if self._sock is None:
            raise RadioError("rfio daemon is not connected")
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self._sock.sendall(data)
        except OSError as exc:
            self.close()
            raise RadioError(f"rfio control send failed: {exc}") from exc

    def _read_line(self) -> dict:
        if self._file is None:
            raise RadioError("rfio daemon is not connected")
        try:
            line = self._file.readline()
        except OSError as exc:
            self.close()
            raise RadioError(f"rfio read failed: {exc}") from exc
        if not line:
            self.close()
            raise RadioError("rfio daemon closed the connection")
        try:
            return json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RadioError(f"rfio protocol error: {exc}") from exc

    def _read_exact(self, count: int) -> bytes:
        if self._file is None:
            raise RadioError("rfio daemon is not connected")
        chunks = []
        remaining = count
        while remaining > 0:
            try:
                chunk = self._file.read(remaining)
            except OSError as exc:
                self.close()
                raise RadioError(f"rfio payload read failed: {exc}") from exc
            if not chunk:
                self.close()
                raise RadioError("rfio daemon closed the connection mid-payload")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
