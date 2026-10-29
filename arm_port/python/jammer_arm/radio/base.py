"""Radio abstraction shared by the UHD, RFIO-daemon and mock backends.

The backend loop only talks to :class:`RadioDevice`.  Swapping the transport
(built-in UHD Python bindings, the C++ ``rfio`` daemon, or a synthetic mock)
therefore never touches sensing, decision making, telemetry or the safety rules.

Safety is enforced here as well as in the loop: :meth:`RadioDevice.transmit`
re-reads the interlock environment variable at call time and validates the frame
before any samples reach hardware.  A device can never transmit because of a
stale boolean captured at start-up.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from typing import Mapping

import numpy as np

from ..config import JammerConfig


class RadioError(RuntimeError):
    """Transport / device failure.  The loop recovers or falls back to cache."""


class InterlockError(RadioError):
    """Raised when transmission is attempted without an armed RF interlock."""


class TxFrameError(RadioError):
    """Raised when a TX frame violates the baseband constraints."""


@dataclass
class RadioInfo:
    """What the device actually applied (UHD may coerce requested values)."""

    backend: str = "unknown"
    opened: bool = False
    sample_rate_hz: float = 0.0
    center_freq_hz: float = 0.0
    rx_gain_db: float = 0.0
    tx_gain_db: float = 0.0
    rx_antenna: str = ""
    tx_antenna: str = ""
    rx_overruns: int = 0
    tx_underruns: int = 0
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "opened": self.opened,
            "sample_rate_hz": self.sample_rate_hz,
            "center_freq_hz": self.center_freq_hz,
            "rx_gain_db": self.rx_gain_db,
            "tx_gain_db": self.tx_gain_db,
            "rx_antenna": self.rx_antenna,
            "tx_antenna": self.tx_antenna,
            "rx_overruns": self.rx_overruns,
            "tx_underruns": self.tx_underruns,
            **({"details": self.details} if self.details else {}),
        }


class RadioDevice(abc.ABC):
    """Minimal device contract used by the frame loop."""

    def __init__(self, cfg: JammerConfig):
        self.cfg = cfg
        self.info = RadioInfo()

    # ── lifecycle ────────────────────────────────────────────────────
    @abc.abstractmethod
    def open(self) -> None:
        """Open the device and configure RX/TX chains."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release all hardware resources.  Must be idempotent."""

    # ── data path ────────────────────────────────────────────────────
    @abc.abstractmethod
    def _receive(self, num_samples: int) -> np.ndarray:
        """Blocking receive of ``num_samples`` complex samples."""

    @abc.abstractmethod
    def _transmit(self, samples: np.ndarray, gain_db: float) -> None:
        """Push one burst.  Only called after :meth:`guard_transmit`."""

    def receive(self, num_samples: int | None = None) -> np.ndarray:
        n = int(num_samples or self.cfg.radio.num_samples)
        samples = self._receive(n)
        samples = np.asarray(samples)
        if samples.dtype != np.complex64 and samples.dtype != np.complex128:
            samples = samples.astype(np.complex64)
        if samples.size < n:
            raise RadioError(f"short receive: got {samples.size} of {n} samples")
        if not np.all(np.isfinite(samples)):
            raise RadioError("receive returned non-finite samples")
        return samples

    def transmit(self, samples: np.ndarray, gain_db: float,
                 env: Mapping[str, str] | None = None) -> str:
        """Validate, then send one frame.  Returns the tx_state string."""
        self.guard_transmit(samples, env=env)
        r = self.cfg.radio
        clamped = float(np.clip(gain_db, r.tx_gain_min_db, r.tx_gain_max_db))
        for _ in range(int(r.tx_repeat)):
            self._transmit(np.asarray(samples, dtype=np.complex64), clamped)
        self.info.tx_gain_db = clamped
        return "running"

    def guard_transmit(self, samples: np.ndarray, env: Mapping[str, str] | None = None) -> None:
        """Fail-closed checks that mirror ``tx_send_frame.m``."""
        env = os.environ if env is None else env
        if not self.cfg.safety.armed(env):
            raise InterlockError(
                "RF TX interlock is not armed. Set "
                f"{self.cfg.safety.interlock_env}={self.cfg.safety.interlock_value} "
                "only in an authorized shielded or cabled setup."
            )
        frame = np.asarray(samples)
        if frame.size == 0:
            raise TxFrameError("TX frame must be non-empty")
        if not np.all(np.isfinite(frame)):
            raise TxFrameError("TX frame must be finite")
        peak = float(np.max(np.abs(frame)))
        limit = self.cfg.radio.tx_baseband_peak + 1e-9
        if peak > limit:
            raise TxFrameError(
                f"TX frame peak {peak:.4f} exceeds configured baseband peak "
                f"{self.cfg.radio.tx_baseband_peak:.4f}"
            )

    # ── recovery ─────────────────────────────────────────────────────
    def rebuild_rx(self) -> None:
        """Recreate the receive path (MATLAB: release + radio_rx_init)."""
        self.close()
        self.open()

    def rebuild(self) -> None:
        """Recreate the whole chain (MATLAB: rebuild_full_radio)."""
        self.close()
        self.open()

    @property
    def is_open(self) -> bool:
        return self.info.opened
