"""All-synthetic backend: port of ``run_mock_backend_spectrum_control.m``.

This is the no-hardware regression path.  Channel features are sampled directly
instead of being sensed from IQ (same distributions, same thresholds as the
MATLAB mock), so the UI, the RL service, the Agent presets and the command bridge
can be exercised end-to-end without a USRP - exactly what the original mock did.
"""

from __future__ import annotations

import numpy as np

from ..config import JammerConfig
from ..dsp.sensing import SensingResult, freq_axis_hz
from ..radio.mock import NullRadio
from .backend import JammerBackend

MOCK_TX_STATE = "mock_running"


class MockBackend(JammerBackend):
    """Synthetic features + synthetic IQ, no device."""

    mode = "mock"

    def __init__(self, cfg: JammerConfig, seed: int | None = None, **kwargs):
        radio = kwargs.pop("radio", None) or NullRadio(cfg)
        super().__init__(cfg, radio, **kwargs)
        self.rng = np.random.default_rng(seed)

    @property
    def frame_period_sec(self) -> float:
        return self.cfg.loop.mock_frame_period_sec

    # ── synthetic sensing ────────────────────────────────────────────
    def _acquire_sensing(self) -> tuple[SensingResult, bool]:
        features, ch_map = self._mock_features(self.state.prev_action)
        iq = self._mock_iq(features)
        n = iq.size
        spec = np.abs(np.fft.fftshift(np.fft.fft(iq)))
        sensing = SensingResult(
            channel_features=features,
            channel_map=ch_map,
            snr_est=float(5.0 + 15.0 * self.rng.random()),
            bw_est=float(10e6 + 10e6 * self.rng.random()),
            sig_type=1,
            spectrum_abs=spec,
            freq_axis_hz=freq_axis_hz(self.cfg.radio.center_freq_hz,
                                      self.cfg.radio.sample_rate_hz, n),
        )
        self.state.last_sensing = sensing
        return sensing, True

    def _transmit(self, frame: np.ndarray, gain_db: float) -> str:
        # The MATLAB mock always reports mock_running; nothing is radiated here.
        return MOCK_TX_STATE

    # ── synthetic model ──────────────────────────────────────────────
    def _mock_features(self, prev_action: dict) -> tuple[np.ndarray, np.ndarray]:
        cfg = self.cfg
        n = cfg.spectrum.num_channels
        features = np.zeros((n, 4))
        ch_map = np.zeros(n, dtype=np.int64)
        for k in range(n):
            occ = float(self.rng.random())
            interf = float(self.rng.random())
            util = 0.2 + 0.8 * float(self.rng.random())
            snr_like = max(0.0, min(1.0, occ * (1 - interf) + 0.1 * float(self.rng.random())))
            features[k] = (occ, interf, util, snr_like)
            if occ < 0.25:
                ch_map[k] = 0
            elif interf > 0.65:
                ch_map[k] = 2
            else:
                ch_map[k] = 1

        centers = cfg.channel_centers_hz
        ch = int(np.clip(int(prev_action["channel_idx"]), 0, n - 1))
        jam_half = float(prev_action["bw_mhz"]) * 1e6 / 2
        ch_half = cfg.spectrum.channel_width_hz / 2
        for k in range(n):
            if abs(centers[k] - centers[ch]) < (jam_half + ch_half):
                if ch_map[k] == 1:
                    ch_map[k] = 3
                elif ch_map[k] == 0:
                    ch_map[k] = 2
        return features, ch_map

    def _mock_iq(self, features: np.ndarray) -> np.ndarray:
        cfg = self.cfg
        n = int(cfg.radio.num_samples)
        t = np.arange(n, dtype=np.float64) / cfg.radio.sample_rate_hz
        iq = 0.05 * (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
        centers = cfg.channel_centers_hz - cfg.radio.center_freq_hz
        for k in range(features.shape[0]):
            occ, interf = float(features[k, 0]), float(features[k, 1])
            amp = 0.1 + 0.5 * occ * (1 - 0.5 * interf)
            iq = iq + amp * np.exp(1j * 2 * np.pi * centers[k] * t)
        return iq
