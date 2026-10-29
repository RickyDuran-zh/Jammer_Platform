"""Numerical equivalence with the original MATLAB implementation.

``tests/data/reference.json`` is produced by
``arm_port/tools/matlab_reference/dump_reference.m`` on the Windows workstation
that runs the MATLAB backend, using the real ``jammer_config.m``,
``sense_channel_grid.m`` and ``generate_jammer_signal.m``.  These tests replay the
same inputs through the ARM port and require the outputs to match.

Regenerate the fixture with::

    matlab -batch "addpath('arm_port/tools/matlab_reference'); dump_reference"

Covered
-------
* sensing  : features, channel map, snr_est, bw_est, sig_type (4 cases)
* waveform : multi-tone mode is deterministic -> exact comparison (3 cases)
* FIR taps : ``fir1(128, Wn)`` vs :func:`design_lowpass` for every bandwidth the
             action space can produce on inner and edge channels (12 designs)
* filtering: ``filter(b, 1, x)`` vs ``convolve(taps, x)[:N]``
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from jammer_arm.config import JammerConfig
from jammer_arm.dsp.sensing import sense_channel_grid
from jammer_arm.dsp.waveform import design_lowpass, generate_jammer_signal

DATA = Path(__file__).resolve().parent / "data" / "reference.json"


def load_reference() -> dict | None:
    if not DATA.is_file():
        return None
    with DATA.open("r", encoding="utf-8") as fh:
        return json.load(fh)


REF = load_reference()


def _cfg() -> JammerConfig:
    return JammerConfig()


@unittest.skipIf(REF is None, "reference.json missing - run dump_reference.m")
class TestMatlabEquivalence(unittest.TestCase):
    def test_matlab_config_matches_arm_defaults(self):
        mc = REF["cfg"]
        cfg = _cfg()
        self.assertAlmostEqual(cfg.radio.center_freq_hz, float(mc["center_freq"]), delta=1.0)
        self.assertAlmostEqual(cfg.radio.sample_rate_hz, float(mc["sample_rate"]), delta=1.0)
        self.assertEqual(cfg.radio.num_samples, int(mc["sample_num"]))
        self.assertAlmostEqual(cfg.radio.master_clock_rate_hz, float(mc["MasterClockRate"]), delta=1.0)
        self.assertEqual(cfg.spectrum.num_channels, int(mc["num_channels"]))
        self.assertAlmostEqual(cfg.spectrum.channel_width_hz, float(mc["channel_width_hz"]), delta=1.0)
        np.testing.assert_allclose(cfg.channel_centers_hz,
                                   np.asarray(mc["channel_centers_hz"], dtype=float),
                                   rtol=0, atol=1.0)
        self.assertEqual(list(cfg.actions.bw_choices_mhz),
                         [int(v) for v in mc["bw_choices_mhz"]])
        self.assertEqual(list(cfg.actions.power_levels_db),
                         [float(v) for v in mc["power_levels_db"]])
        self.assertAlmostEqual(cfg.radio.tx_baseband_peak, float(mc["tx_baseband_peak"]), places=12)
        self.assertAlmostEqual(cfg.radio.rx_gain_db, float(mc["rx_gain"]), places=12)
        self.assertAlmostEqual(cfg.radio.tx_gain_max_db, float(mc["tx_gain_max"]), places=12)
        self.assertEqual(cfg.network.rl_port, int(mc["rl_port"]))
        self.assertEqual(cfg.network.telemetry_port, int(mc["ui_port"]))
        self.assertEqual(cfg.network.command_port, int(mc["cmd_port"]))

    def test_sensing_cases(self):
        cfg = _cfg()
        cases = [c for c in REF["cases"] if c["kind"] == "sensing"]
        self.assertGreaterEqual(len(cases), 4)
        for case in cases:
            with self.subTest(case=case["name"]):
                rx = np.asarray(case["rx_re"]) + 1j * np.asarray(case["rx_im"])
                prev = case["prev_action"]
                res = sense_channel_grid(rx, cfg, prev)
                np.testing.assert_allclose(
                    res.channel_features,
                    np.asarray(case["channel_features"], dtype=float),
                    rtol=0, atol=1e-12,
                    err_msg="channel_features differ from MATLAB")
                np.testing.assert_array_equal(
                    res.channel_map,
                    np.asarray(case["channel_map"], dtype=np.int64),
                    err_msg="channel_map differs from MATLAB")
                self.assertAlmostEqual(res.snr_est, float(case["snr_est"]), delta=1e-9)
                self.assertAlmostEqual(res.bw_est, float(case["bw_est"]), delta=1e-6)
                self.assertEqual(res.sig_type, int(case["sig_type"]))
                np.testing.assert_allclose(
                    res.spectrum_abs,
                    np.asarray(case["spectrum_abs"], dtype=float),
                    rtol=1e-9, atol=1e-9)

    def test_multitone_waveforms_are_bit_identical(self):
        cfg = _cfg()
        for case in [c for c in REF["cases"] if c["kind"] == "waveform"]:
            with self.subTest(case=case["name"]):
                y, gain = generate_jammer_signal(case["action"], cfg)
                ref = np.asarray(case["y_re"]) + 1j * np.asarray(case["y_im"])
                np.testing.assert_allclose(y, ref, rtol=1e-10, atol=1e-12)
                self.assertAlmostEqual(gain, float(case["tx_gain"]), places=9)

    def test_fir_designs_match_fir1(self):
        for entry in REF["fir_designs"]["entries"]:
            with self.subTest(bw=entry["bw_mhz"], ch=entry["channel_idx"]):
                taps = design_lowpass(129, float(entry["norm_cutoff"]))
                ref = np.asarray(entry["taps"], dtype=float)
                self.assertEqual(taps.size, ref.size)
                np.testing.assert_allclose(taps, ref, rtol=1e-10, atol=1e-13)

    def test_filter_call_matches_matlab_filter(self):
        test = REF["fir_filter_test"]
        taps = np.asarray(test["taps"], dtype=float)
        x = np.asarray(test["x_re"]) + 1j * np.asarray(test["x_im"])
        expected = np.asarray(test["y_re"]) + 1j * np.asarray(test["y_im"])
        got = np.convolve(taps, x)[:x.size]
        np.testing.assert_allclose(got, expected, rtol=1e-11, atol=1e-13)


if __name__ == "__main__":
    unittest.main()
