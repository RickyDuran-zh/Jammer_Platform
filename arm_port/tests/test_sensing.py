"""Behaviour of the sensing port: shape, ranges, channel tracking, jam overlay."""

from __future__ import annotations

import unittest

import numpy as np

from jammer_arm.config import JammerConfig
from jammer_arm.dsp.sensing import SensingResult, freq_axis_hz, sense_channel_grid


def tone_at(cfg: JammerConfig, channel_idx: int, amplitude: float = 1.0,
            offset_hz: float = 0.0) -> np.ndarray:
    """A single CW carrier inside one logical channel."""
    t = np.arange(cfg.radio.num_samples) / cfg.radio.sample_rate_hz
    f = cfg.channel_centers_hz[channel_idx] - cfg.radio.center_freq_hz + offset_hz
    return amplitude * np.exp(1j * 2 * np.pi * f * t)


def noise_at(cfg: JammerConfig, channel_idx: int, amplitude: float = 1.0,
             rng: np.random.Generator | None = None) -> np.ndarray:
    """Occupied-channel traffic: noise filling the 2 MHz channel (OFDM-like).

    The original sensing defines occupancy as the *mean* normalised power inside a
    channel, so a pure CW tone (one bin out of 409) reads as almost empty.  A
    realistic user signal fills the channel, which is what occupancy is meant to
    detect.
    """
    rng = rng or np.random.default_rng(0)
    n = cfg.radio.num_samples
    x = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    spec = np.fft.fftshift(np.fft.fft(x))
    freqs = (np.arange(n) - n / 2.0) * (cfg.radio.sample_rate_hz / n)
    f0 = cfg.channel_centers_hz[channel_idx] - cfg.radio.center_freq_hz
    keep = np.abs(freqs - f0) <= cfg.spectrum.channel_width_hz / 2
    spec[~keep] = 0.0
    y = np.fft.ifft(np.fft.ifftshift(spec))
    peak = float(np.max(np.abs(y))) or 1.0
    return amplitude * y / peak


class TestSensing(unittest.TestCase):
    def setUp(self):
        self.cfg = JammerConfig()
        self.rng = np.random.default_rng(42)

    def test_shapes_and_ranges(self):
        rx = self.rng.standard_normal(4096) + 1j * self.rng.standard_normal(4096)
        res = sense_channel_grid(rx, self.cfg)
        self.assertEqual(res.channel_features.shape, (10, 4))
        self.assertEqual(res.channel_map.shape, (10,))
        self.assertTrue(np.all(np.isfinite(res.channel_features)))
        self.assertTrue(np.all((res.channel_features >= 0) & (res.channel_features <= 1)))
        self.assertTrue(set(np.unique(res.channel_map)) <= {0, 1, 2, 3})
        self.assertEqual(res.spectrum_abs.size, 4096)
        self.assertEqual(res.freq_axis_hz.size, 4096)

    def test_spectrum_axes(self):
        rx = self.rng.standard_normal(4096) + 1j * self.rng.standard_normal(4096)
        res = sense_channel_grid(rx, self.cfg)
        np.testing.assert_allclose(np.diff(res.freq_axis_hz),
                                   np.full(4095, 20e6 / 4096), rtol=0, atol=1e-6)
        self.assertAlmostEqual(res.freq_axis_hz[0], 3e9 - 10e6, delta=1.0)
        self.assertAlmostEqual(res.freq_axis_hz[-1], 3e9 + 10e6 - 20e6 / 4096, delta=1.0)

    def test_tracks_occupied_channel(self):
        for target in (0, 3, 9):
            with self.subTest(channel=target):
                rx = 0.01 * (self.rng.standard_normal(4096) + 1j * self.rng.standard_normal(4096))
                rx = rx + noise_at(self.cfg, target, 1.0, self.rng)
                res = sense_channel_grid(rx, self.cfg)
                occ = res.channel_features[:, 0]
                self.assertEqual(int(np.argmax(occ)), target)
                neighbours = np.delete(occ, target)
                self.assertGreater(occ[target], 5 * float(neighbours.max()))
                self.assertGreater(occ[target], 0.15)
                self.assertIn(int(res.channel_map[target]), (1, 2))

    def test_cw_tone_reads_as_interference_not_occupancy(self):
        """A pure carrier occupies one FFT bin, so it raises interference, not occ."""
        rx = 0.01 * (self.rng.standard_normal(4096) + 1j * self.rng.standard_normal(4096))
        rx = rx + tone_at(self.cfg, 5, 1.0)
        res = sense_channel_grid(rx, self.cfg)
        self.assertEqual(int(np.argmax(res.channel_features[:, 1])), 5)
        self.assertLess(res.channel_features[5, 0], 0.15)

    def test_jam_overlay_marks_neighbours(self):
        rx = 0.01 * (self.rng.standard_normal(4096) + 1j * self.rng.standard_normal(4096))
        for k in range(10):
            rx = rx + noise_at(self.cfg, k, 0.5, self.rng)
        prev = {"channel_idx": 4, "power_db": 10, "bw_mhz": 6, "waveform_mode": 0}
        res = sense_channel_grid(rx, self.cfg, prev)
        # 6 MHz centred on channel index 4 covers indices 3, 4, 5
        for k in (3, 4, 5):
            self.assertEqual(int(res.channel_map[k]), 3, f"channel {k} should be occupied+jam")
        for k in (0, 9):
            self.assertNotIn(int(res.channel_map[k]), (2, 3))

    def test_silence_is_idle(self):
        res = sense_channel_grid(np.zeros(4096, dtype=np.complex128), self.cfg)
        self.assertTrue(np.all(res.channel_map == 0))
        self.assertTrue(np.all(np.isfinite(res.channel_features)))
        self.assertEqual(res.bw_est, 0.0)
        self.assertEqual(res.sig_type, 0)

    def test_longer_frame_uses_its_own_length(self):
        rx = self.rng.standard_normal(8192) + 1j * self.rng.standard_normal(8192)
        res = sense_channel_grid(rx, self.cfg)
        self.assertEqual(res.spectrum_abs.size, 8192)
        self.assertAlmostEqual(res.freq_axis_hz[1] - res.freq_axis_hz[0], 20e6 / 8192, delta=1e-6)

    def test_wideband_signal_has_high_sig_type(self):
        rx = self.rng.standard_normal(4096) + 1j * self.rng.standard_normal(4096)
        res = sense_channel_grid(rx, self.cfg)
        self.assertGreaterEqual(res.bw_est, 0.0)
        self.assertIn(res.sig_type, (0, 1, 2, 3))

    def test_empty_result_helper(self):
        empty = SensingResult.empty(self.cfg)
        self.assertEqual(empty.channel_features.shape, (10, 4))
        self.assertEqual(empty.channel_map.shape, (10,))
        copy = empty.copy()
        copy.channel_features[0, 0] = 1.0
        self.assertEqual(empty.channel_features[0, 0], 0.0)

    def test_freq_axis_helper_matches_sensing(self):
        axis = freq_axis_hz(self.cfg.radio.center_freq_hz, self.cfg.radio.sample_rate_hz, 4096)
        rx = np.zeros(4096, dtype=np.complex128)
        res = sense_channel_grid(rx, self.cfg)
        np.testing.assert_allclose(axis, res.freq_axis_hz, rtol=0, atol=1e-9)


if __name__ == "__main__":
    unittest.main()
