"""Synthetic radio: USRP-shaped behaviour without hardware.

Two uses:

* ``--radio mock`` - replaces only the device.  The real sensing and waveform
  code still runs, so the DSP chain is exercised end-to-end on a laptop or on the
  board before an X310 is available.
* tests - fault injection (``fail_next``) drives the RX recovery ladder.

Nothing here ever emits RF: the TX path only records frames.
"""

from __future__ import annotations

import threading

import numpy as np

from ..config import JammerConfig
from .base import RadioDevice, RadioError


class SyntheticSource:
    """Slowly drifting 10-channel occupancy used to synthesise IQ."""

    def __init__(self, cfg: JammerConfig, seed: int | None = None):
        self.cfg = cfg
        self.rng = np.random.default_rng(seed)
        n = cfg.spectrum.num_channels
        self.occupancy = self.rng.random(n) * 0.5
        self._centers = cfg.channel_centers_hz - cfg.radio.center_freq_hz

    def advance(self) -> np.ndarray:
        alpha = 0.15
        noise = self.rng.normal(0.0, 0.12, self.occupancy.size)
        self.occupancy = np.clip(
            (1 - alpha) * self.occupancy + alpha * (0.25 + 0.6 * self.rng.random(self.occupancy.size))
            + noise, 0.0, 1.0)
        return self.occupancy

    def build_iq(self, num_samples: int) -> np.ndarray:
        """Noise floor plus one complex tone per occupied channel."""
        rng = self.rng
        t = np.arange(num_samples, dtype=np.float64) / self.cfg.radio.sample_rate_hz
        iq = 0.02 * (rng.standard_normal(num_samples) + 1j * rng.standard_normal(num_samples))
        for k, amp in enumerate(self.occupancy):
            if amp < 0.05:
                continue
            phase = rng.uniform(0, 2 * np.pi)
            iq += (0.1 + 0.5 * amp) * np.exp(
                1j * (2 * np.pi * self._centers[k] * t + phase))
        return iq.astype(np.complex64)


class MockRadio(RadioDevice):
    """A device that stores TX frames instead of radiating them."""

    def __init__(self, cfg: JammerConfig, seed: int | None = None):
        super().__init__(cfg)
        self.info.backend = "mock"
        self.source = SyntheticSource(cfg, seed=seed)
        self._lock = threading.Lock()
        self.tx_frames: list[dict] = []
        self.receive_count = 0
        self._pending_failures = 0
        self._fail_always = False

    # ── lifecycle ────────────────────────────────────────────────────
    def open(self) -> None:
        self.info.opened = True
        self.info.sample_rate_hz = self.cfg.radio.sample_rate_hz
        self.info.center_freq_hz = self.cfg.radio.center_freq_hz
        self.info.rx_gain_db = self.cfg.radio.rx_gain_db
        self.info.rx_antenna = self.cfg.radio.rx_antenna
        self.info.tx_antenna = self.cfg.radio.tx_antenna

    def close(self) -> None:
        self.info.opened = False

    # ── fault injection (tests) ──────────────────────────────────────
    def fail_next(self, count: int = 1) -> None:
        with self._lock:
            self._pending_failures += int(count)

    def fail_always(self, enabled: bool = True) -> None:
        with self._lock:
            self._fail_always = bool(enabled)

    # ── data path ────────────────────────────────────────────────────
    def _receive(self, num_samples: int) -> np.ndarray:
        with self._lock:
            if self._fail_always:
                raise RadioError("injected permanent RX failure")
            if self._pending_failures > 0:
                self._pending_failures -= 1
                raise RadioError("injected RX timeout")
        self.receive_count += 1
        self.source.advance()
        return self.source.build_iq(num_samples)

    def _transmit(self, samples: np.ndarray, gain_db: float) -> None:
        with self._lock:
            self.tx_frames.append({
                "num_samples": int(samples.size),
                "gain_db": float(gain_db),
                "peak": float(np.max(np.abs(samples))) if samples.size else 0.0,
                "mean_power": float(np.mean(np.abs(samples) ** 2)) if samples.size else 0.0,
            })
            if len(self.tx_frames) > 512:
                self.tx_frames.pop(0)


class NullRadio(RadioDevice):
    """No device at all (used by the all-synthetic ``mock`` command)."""

    def __init__(self, cfg: JammerConfig):
        super().__init__(cfg)
        self.info.backend = "none"

    def open(self) -> None:
        self.info.opened = True

    def close(self) -> None:
        self.info.opened = False

    def _receive(self, num_samples: int) -> np.ndarray:
        raise RadioError("NullRadio cannot receive")

    def _transmit(self, samples: np.ndarray, gain_db: float) -> None:
        self.info.tx_gain_db = float(gain_db)
