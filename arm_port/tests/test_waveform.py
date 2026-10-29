"""Waveform port: peak normalisation, band limiting, tone placement, gain mapping."""

from __future__ import annotations

import unittest

import numpy as np

from jammer_arm.config import JammerConfig
from jammer_arm.dsp.waveform import (
    design_lowpass,
    effective_bandwidth_hz,
    generate_jammer_signal,
    tx_gain_for_power,
    waveform_summary,
)


def band_power_fraction(y: np.ndarray, fs: float, center_hz: float, half_bw_hz: float) -> float:
    spec = np.abs(np.fft.fftshift(np.fft.fft(y))) ** 2
    freqs = (np.arange(y.size) - y.size / 2.0) * (fs / y.size)
    inside = np.abs(freqs - center_hz) <= half_bw_hz
    return float(spec[inside].sum() / (spec.sum() + 1e-30))


class TestWaveform(unittest.TestCase):
    def setUp(self):
        self.cfg = JammerConfig()
        self.fs = self.cfg.radio.sample_rate_hz

    def _action(self, **kw) -> dict:
        base = {"channel_idx": 4, "power_db": 10, "bw_mhz": 4, "waveform_mode": 0}
        base.update(kw)
        return base

    def test_peak_is_normalised(self):
        for mode in (0, 1):
            for ch in (0, 4, 9):
                with self.subTest(mode=mode, ch=ch):
                    y, _ = generate_jammer_signal(
                        self._action(channel_idx=ch, waveform_mode=mode), self.cfg)
                    self.assertEqual(y.size, self.cfg.radio.num_samples)
                    self.assertAlmostEqual(float(np.max(np.abs(y))), 0.25, places=12)
                    self.assertTrue(np.all(np.isfinite(y)))

    def test_noise_is_band_limited(self):
        for ch in (0, 4, 9):
            for bw in (2, 4, 6, 8):
                with self.subTest(ch=ch, bw=bw):
                    y, _ = generate_jammer_signal(
                        self._action(channel_idx=ch, bw_mhz=bw), self.cfg,
                        rng=np.random.default_rng(1))
                    f0 = self.cfg.channel_centers_hz[ch] - self.cfg.radio.center_freq_hz
                    bw_eff = effective_bandwidth_hz(bw, f0, self.fs)
                    frac = band_power_fraction(y, self.fs, f0, bw_eff / 2 * 1.15)
                    self.assertGreater(frac, 0.9, f"{frac:.3f} of the power inside {bw_eff/1e6:.1f} MHz")

    def test_fft_shaping_also_band_limited(self):
        y, _ = generate_jammer_signal(self._action(), self.cfg, shaping="fft",
                                      rng=np.random.default_rng(2))
        f0 = self.cfg.channel_centers_hz[4] - self.cfg.radio.center_freq_hz
        self.assertGreater(band_power_fraction(y, self.fs, f0, 2.6e6), 0.9)
        self.assertAlmostEqual(float(np.max(np.abs(y))), 0.25, places=12)

    def test_unknown_shaping_rejected(self):
        with self.assertRaises(ValueError):
            generate_jammer_signal(self._action(), self.cfg, shaping="brick")

    def test_multitone_has_four_components(self):
        y, _ = generate_jammer_signal(self._action(waveform_mode=1, bw_mhz=4), self.cfg)
        spec = np.abs(np.fft.fftshift(np.fft.fft(y)))
        threshold = 0.3 * spec.max()
        above = spec > threshold
        # count contiguous runs above threshold
        runs = int(np.count_nonzero(np.diff(above.astype(int)) == 1) + (1 if above[0] else 0))
        self.assertEqual(runs, 4)

    def test_multitone_is_deterministic(self):
        a = self._action(waveform_mode=1)
        y1, _ = generate_jammer_signal(a, self.cfg)
        y2, _ = generate_jammer_signal(a, self.cfg)
        np.testing.assert_array_equal(y1, y2)

    def test_edge_channel_bandwidth_is_clipped(self):
        f0 = self.cfg.channel_centers_hz[9] - self.cfg.radio.center_freq_hz
        self.assertAlmostEqual(effective_bandwidth_hz(8, f0, self.fs), 2e6, delta=1.0)
        self.assertAlmostEqual(effective_bandwidth_hz(8, 0.0, self.fs), 8e6, delta=1.0)
        y, _ = generate_jammer_signal(self._action(channel_idx=9, bw_mhz=8), self.cfg,
                                      rng=np.random.default_rng(3))
        self.assertGreater(band_power_fraction(y, self.fs, f0, 1.2e6), 0.85)

    def test_gain_mapping(self):
        self.assertEqual(tx_gain_for_power(0, self.cfg), 0.0)
        self.assertEqual(tx_gain_for_power(10, self.cfg), 5.0)
        self.assertEqual(tx_gain_for_power(20, self.cfg), 10.0)   # clamped at tx_gain_max
        self.assertEqual(tx_gain_for_power(-5, self.cfg), 0.0)
        self.assertEqual(tx_gain_for_power(40, self.cfg), 10.0)
        _, gain = generate_jammer_signal(self._action(power_db=20), self.cfg)
        self.assertEqual(gain, self.cfg.radio.tx_gain_max_db)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            generate_jammer_signal(self._action(waveform_mode=2), self.cfg)

    def test_num_samples_is_respected(self):
        cfg = JammerConfig.load(None, {"radio.num_samples": 1024})
        y, _ = generate_jammer_signal(self._action(), cfg)
        self.assertEqual(y.size, 1024)

    def test_design_lowpass_properties(self):
        taps = design_lowpass(129, 0.2)
        self.assertEqual(taps.size, 129)
        self.assertAlmostEqual(float(taps.sum()), 1.0, places=12)
        self.assertTrue(np.allclose(taps, taps[::-1], atol=1e-15))   # symmetric
        spec = np.abs(np.fft.rfft(taps, 1024))
        self.assertLess(spec[-1], 0.01 * spec[0])                      # stop band

    def test_summary_reports_bandwidth(self):
        y, _ = generate_jammer_signal(self._action(bw_mhz=6), self.cfg,
                                      rng=np.random.default_rng(4))
        summary = waveform_summary(y, self.fs)
        self.assertGreater(summary["occupied_bw_hz"], 2.0e6)
        self.assertLess(summary["occupied_bw_hz"], 12.0e6)
        self.assertGreater(summary["rms"], 0.0)


if __name__ == "__main__":
    unittest.main()
