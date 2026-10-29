"""UHD backend: USRP X310 driven from the UHD Python API (``import uhd``).

This replaces ``comm.SDRuReceiver`` / ``comm.SDRuTransmitter``.  The mapping is
one-to-one with the MATLAB code:

=================================  ==========================================
MATLAB (Communications Toolbox)    UHD
=================================  ==========================================
Platform 'X310', IPAddress          ``device_args="type=x300,addr=..."``
MasterClockRate = 200e6             ``set_master_clock_rate(200e6)``
DecimationFactor / InterpFactor=10  ``set_rx_rate(20e6)`` / ``set_tx_rate(20e6)``
CenterFrequency                     ``set_rx_freq`` / ``set_tx_freq``
Gain / rx_gain                      ``set_rx_gain`` / ``set_tx_gain``
Antenna 'TX/RX'                     ``set_rx_antenna`` / ``set_tx_antenna``
ClockSource 'Internal'              ``set_clock_source("internal")``
SamplesPerFrame 4096                ``StreamArgs`` + ``recv(buf, md, timeout)``
radio_tx(frame)                     ``tx_stream.send(buff, md, timeout)``
=================================  ==========================================

The streamers are created once.  RX runs continuously (``STREAM_MODE_START_CONTINUOUS``);
TX pushes single bursts.  With ``radio.tdm = true`` (default, matching the
"先 RX 后 TX" contract of the MATLAB backend) the RX stream is paused while a TX
burst is on air so the transmitter cannot desensitise the receiver on the shared
``TX/RX`` port.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from ..config import JammerConfig
from .base import RadioDevice, RadioError

_UHD_IMPORT_HINT = (
    "The UHD Python bindings are not importable. Build UHD with "
    "-DENABLE_PYTHON_API=ON (see arm_port/scripts/build_uhd.sh) and make sure "
    "PYTHONPATH contains <prefix>/lib/python3.x/site-packages, or start the "
    "backend with --radio rfio to use the C++ rfio daemon instead."
)


class UhdRadio(RadioDevice):
    """USRP X310 via the UHD Python API."""

    def __init__(self, cfg: JammerConfig):
        super().__init__(cfg)
        self.info.backend = "uhd"
        self._uhd: Any = None
        self._usrp: Any = None
        self._rx_stream: Any = None
        self._tx_stream: Any = None
        self._md_rx: Any = None
        self._md_tx: Any = None
        self._buf: np.ndarray | None = None
        self._rx_running = False

    # ── lifecycle ────────────────────────────────────────────────────
    def _import_uhd(self):
        try:
            import uhd  # noqa: PLC0415  (optional native dependency)
        except Exception as exc:  # pragma: no cover - depends on target image
            raise RadioError(f"{_UHD_IMPORT_HINT} (original error: {exc})") from exc
        return uhd

    def open(self) -> None:
        self.close()
        uhd = self._import_uhd()
        self._uhd = uhd
        r = self.cfg.radio

        try:
            self._usrp = uhd.usrp.MultiUSRP(r.device_args)
        except Exception as exc:
            raise RadioError(
                f"cannot open USRP with args '{r.device_args}': {exc}. "
                "Check the network address, the FPGA image and `uhd_find_devices`."
            ) from exc

        try:
            self._usrp.set_master_clock_rate(r.master_clock_rate_hz)
            if r.clock_source:
                self._usrp.set_clock_source(r.clock_source)

            # ── RX chain ────────────────────────────────────────────
            self._usrp.set_rx_rate(r.sample_rate_hz, 0)
            self._usrp.set_rx_freq(uhd.types.TuneRequest(r.center_freq_hz), 0)
            _set_gain(self._usrp, "rx", r.rx_gain_db)
            if r.rx_antenna:
                self._usrp.set_rx_antenna(r.rx_antenna, 0)

            # ── TX chain ────────────────────────────────────────────
            self._usrp.set_tx_rate(r.sample_rate_hz, 0)
            self._usrp.set_tx_freq(uhd.types.TuneRequest(r.center_freq_hz), 0)
            _set_gain(self._usrp, "tx", r.tx_gain_base_db)
            if r.tx_antenna:
                self._usrp.set_tx_antenna(r.tx_antenna, 0)

            # ── streamers ───────────────────────────────────────────
            st_args = uhd.usrp.StreamArgs("fc32", "sc16")
            st_args.channels = [0]
            self._rx_stream = self._usrp.get_rx_stream(st_args)
            self._tx_stream = self._usrp.get_tx_stream(st_args)

            self._md_rx = uhd.types.RXMetadata()
            self._md_tx = uhd.types.TXMetadata()
            self._buf = np.zeros(int(r.num_samples), dtype=np.complex64)

            # Give the PLL and the calibration routines time to settle before the
            # first burst (MATLAB: pause(0.5) in run_jammer_backend.m).
            time.sleep(float(r.settle_sec))
            if r.rx_mode == "continuous":
                self._start_rx()
                self._drain_rx(timeout=0.05)
        except RadioError:
            raise
        except Exception as exc:
            self.close()
            raise RadioError(f"USRP configuration failed: {exc}") from exc

        self.info.opened = True
        self._refresh_info()

    def close(self) -> None:
        for attr in ("_rx_stream", "_tx_stream"):
            stream = getattr(self, attr)
            if stream is not None:
                try:
                    if attr == "_rx_stream":
                        self._stop_rx()
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._usrp is not None:
            try:
                del self._usrp
            except Exception:
                pass
            self._usrp = None
        self._buf = None
        self._rx_running = False
        self.info.opened = False

    def rebuild_rx(self) -> None:
        """Restart the RX stream without touching the TX chain."""
        if self._usrp is None:
            self.open()
            return
        try:
            self._stop_rx()
            self._rx_stream = None
            st_args = self._uhd.usrp.StreamArgs("fc32", "sc16")
            st_args.channels = [0]
            self._rx_stream = self._usrp.get_rx_stream(st_args)
            self._start_rx()
            time.sleep(self.cfg.loop.rx_reinit_pause_sec)
        except Exception as exc:
            raise RadioError(f"RX rebuild failed: {exc}") from exc

    # ── streaming helpers ────────────────────────────────────────────
    def _start_rx(self) -> None:
        cmd = self._uhd.types.StreamCMD(self._uhd.types.StreamMode.start_cont)
        cmd.stream_now = True
        self._rx_stream.issue_stream_cmd(cmd)
        self._rx_running = True

    def _stop_rx(self) -> None:
        if self._rx_stream is None or not self._rx_running:
            return
        cmd = self._uhd.types.StreamCMD(self._uhd.types.StreamMode.stop_cont)
        self._rx_stream.issue_stream_cmd(cmd)
        self._rx_running = False

    def _request_burst(self, num_samples: int) -> None:
        """Ask for exactly ``num_samples`` and let the device stop by itself.

        This is what makes a ~12 fps frame loop compatible with a 20 MS/s device:
        the host only ever has one frame's worth of samples queued, so there is
        nothing to overflow between frames.  A continuous stream would produce
        20 MSa/s while the loop consumes ~34 kSa/s.
        """
        cmd = self._uhd.types.StreamCMD(self._uhd.types.StreamMode.num_samps_and_done)
        cmd.stream_now = True
        cmd.num_samps = int(num_samples)
        self._rx_stream.issue_stream_cmd(cmd)
        # Nothing to stop later: the device ends the burst on its own.

    def _drain_rx(self, timeout: float = 0.0) -> None:
        """Discard whatever is queued so the next frame is fresh."""
        if self._rx_stream is None or self._buf is None:
            return
        try:
            self._rx_stream.recv(self._buf, self._md_rx, timeout)
        except Exception:
            pass

    def _receive(self, num_samples: int) -> np.ndarray:
        if self._rx_stream is None or self._usrp is None:
            raise RadioError("RX stream is not open")
        if self._buf is None or self._buf.size != num_samples:
            self._buf = np.zeros(num_samples, dtype=np.complex64)

        burst = self.cfg.radio.rx_mode != "continuous"
        timeout = float(self.cfg.radio.recv_timeout_sec)
        deadline = time.monotonic() + timeout + 1.0
        filled = 0

        if burst:
            self._request_burst(num_samples)
        elif not self._rx_running:
            self._start_rx()

        while filled < num_samples:
            try:
                n = self._rx_stream.recv(
                    self._buf[filled:] if filled else self._buf, self._md_rx,
                    max(0.01, deadline - time.monotonic()))
            except Exception as exc:
                raise RadioError(f"recv failed: {exc}") from exc

            code = _error_code_name(self._md_rx)
            if code == "timeout":
                if filled:
                    raise RadioError(f"short recv: {filled}/{num_samples} before timeout")
                raise RadioError("recv timeout (no samples from device)")
            if code == "overflow":
                self.info.rx_overruns += 1
                raise RadioError("recv overflow (host could not keep up with the device)")
            if code == "late":
                raise RadioError("recv late command")
            if code not in ("none", ""):
                raise RadioError(f"recv error: {code}")
            if n == 0:
                if time.monotonic() > deadline:
                    raise RadioError(f"short recv: {filled}/{num_samples} before deadline")
                continue
            filled += n
            if burst:
                break         # NUM_SAMPS_AND_DONE delivers the whole burst at once

        if filled < num_samples:
            # Never hand a partially filled buffer to the DSP: the tail is stale.
            raise RadioError(f"short recv: {filled}/{num_samples}")
        return np.array(self._buf[:num_samples], dtype=np.complex64)

    def _transmit(self, samples: np.ndarray, gain_db: float) -> None:
        if self._usrp is None or self._tx_stream is None:
            raise RadioError("TX stream is not open")
        try:
            _set_gain(self._usrp, "tx", gain_db)
        except Exception as exc:
            raise RadioError(f"cannot set TX gain: {exc}") from exc

        # Only a continuous RX stream needs pausing; in burst mode the device has
        # already stopped after delivering the requested frame.
        tdm = bool(self.cfg.radio.tdm) and self.cfg.radio.rx_mode == "continuous"
        if tdm:
            self._stop_rx()
            time.sleep(0.002)

        meta = self._md_tx
        meta.start_of_burst = True
        meta.end_of_burst = True
        buf = np.asarray(samples, dtype=np.complex64).reshape(1, -1)
        offset = 0
        timeout = float(self.cfg.radio.recv_timeout_sec)
        try:
            while offset < buf.shape[1]:
                sent = self._tx_stream.send(buf[:, offset:], meta, timeout)
                if sent == 0:
                    raise RadioError("TX send returned 0 samples (underrun)")
                offset += int(sent)
                meta.start_of_burst = False
        except RadioError:
            raise
        except Exception as exc:
            raise RadioError(f"TX send failed: {exc}") from exc
        finally:
            if tdm:
                try:
                    self._start_rx()
                except Exception:
                    self._rx_running = False

    # ── diagnostics ──────────────────────────────────────────────────
    def _refresh_info(self) -> None:
        usrp = self._usrp
        if usrp is None:
            return
        getters = {
            "sample_rate_hz": ("get_rx_rate", (0,)),
            "center_freq_hz": ("get_rx_freq", (0,)),
            "rx_gain_db": ("get_rx_gain", ("", 0)),
            "tx_gain_db": ("get_tx_gain", ("", 0)),
            "rx_antenna": ("get_rx_antenna", (0,)),
            "tx_antenna": ("get_tx_antenna", (0,)),
        }
        for attr, (method, args) in getters.items():
            fn = getattr(usrp, method, None)
            if fn is None:
                continue
            try:
                value = fn(*args)
            except TypeError:
                try:
                    value = fn(*args[:1])
                except Exception:
                    continue
            except Exception:
                continue
            setattr(self.info, attr, value)
        try:
            self.info.details.update({
                "mboard": usrp.get_mboard_name(0),
                "rx_subdev": usrp.get_rx_subdev_name(0),
                "master_clock_rate_hz": usrp.get_master_clock_rate(0),
                "rx_rate_hz_actual": usrp.get_rx_rate(0),
                "tx_rate_hz_actual": usrp.get_tx_rate(0),
            })
        except Exception:
            pass


def _set_gain(usrp: Any, direction: str, gain: float) -> None:
    """``set_rx_gain(gain, name, chan)`` with fallbacks across UHD versions."""
    fn = getattr(usrp, f"set_{direction}_gain", None)
    if fn is None:
        raise RadioError(f"UHD object has no set_{direction}_gain")
    attempts = ((float(gain), "", 0), (float(gain), 0), (float(gain),))
    last: Exception | None = None
    for args in attempts:
        try:
            fn(*args)
            return
        except TypeError as exc:
            last = exc
        except Exception as exc:  # noqa: BLE001 - re-raised below
            last = exc
    raise RadioError(f"set_{direction}_gain({gain}) failed: {last}")


def _error_code_name(metadata: Any) -> str:
    try:
        code = metadata.error_code
    except Exception:
        return "none"
    name = getattr(code, "name", None)
    if isinstance(name, str):
        return name.lower()
    text = str(code)
    return text.rsplit(".", 1)[-1].lower()
