"""Configuration contract: defaults, validation, overrides and the TX interlock."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from jammer_arm.config import ConfigError, JammerConfig, parse_set_args


class TestDefaults(unittest.TestCase):
    def setUp(self):
        self.cfg = JammerConfig()

    def test_channel_grid_matches_matlab(self):
        centers = self.cfg.channel_centers_hz
        self.assertEqual(centers.size, 10)
        self.assertAlmostEqual(centers[0], 3.0e9 - 9e6, delta=1.0)
        self.assertAlmostEqual(centers[-1], 3.0e9 + 9e6, delta=1.0)
        np.testing.assert_allclose(np.diff(centers), np.full(9, 2e6), rtol=0, atol=1e-6)

    def test_sampling_chain(self):
        self.assertAlmostEqual(self.cfg.radio.master_clock_rate_hz / self.cfg.radio.sample_rate_hz,
                               10.0, places=9)
        self.assertEqual(self.cfg.radio.num_samples, 4096)
        self.assertAlmostEqual(self.cfg.bin_width_hz, 20e6 / 4096, places=6)

    def test_ports_unchanged(self):
        self.assertEqual(self.cfg.network.rl_port, 6001)
        self.assertEqual(self.cfg.network.telemetry_port, 5555)
        self.assertEqual(self.cfg.network.command_port, 5557)

    def test_action_space(self):
        self.assertEqual(self.cfg.actions.bw_choices_mhz, (2, 4, 6, 8))
        self.assertEqual(self.cfg.actions.waveform_modes, (0, 1))
        self.assertEqual(self.cfg.actions.power_db_min, 0.0)
        self.assertEqual(self.cfg.actions.power_db_max, 20.0)


class TestInterlock(unittest.TestCase):
    def test_disarmed_by_default(self):
        cfg = JammerConfig()
        self.assertFalse(cfg.tx_armed(env={}))
        self.assertFalse(cfg.tx_armed(env={"ZH_RF_TX_ENABLE": ""}))
        self.assertFalse(cfg.tx_armed(env={"ZH_RF_TX_ENABLE": "1"}))
        self.assertFalse(cfg.tx_armed(env={"ZH_RF_TX_ENABLE": "lab_only"}))

    def test_armed_only_by_exact_env(self):
        cfg = JammerConfig()
        self.assertTrue(cfg.tx_armed(env={"ZH_RF_TX_ENABLE": "LAB_ONLY"}))

    def test_env_is_read_at_call_time(self):
        cfg = JammerConfig()
        os.environ.pop("ZH_RF_TX_ENABLE", None)
        self.assertFalse(cfg.tx_armed())
        os.environ["ZH_RF_TX_ENABLE"] = "LAB_ONLY"
        try:
            self.assertTrue(cfg.tx_armed())
        finally:
            os.environ.pop("ZH_RF_TX_ENABLE", None)

    def test_config_file_cannot_arm_tx(self):
        for forbidden in ("tx_enabled", "interlock_env", "interlock_value", "tx_baseband_peak"):
            with self.subTest(key=forbidden):
                with self.assertRaises(ConfigError):
                    JammerConfig.from_dict({forbidden: True})

    def test_config_file_cannot_arm_tx_inside_section(self):
        with self.assertRaises(ConfigError):
            JammerConfig.from_dict({"safety": {"interlock_value": "ANYTHING"}})


class TestOverrides(unittest.TestCase):
    def test_dotted_override(self):
        cfg = JammerConfig.load(None, {"radio.num_samples": 8192})
        self.assertEqual(cfg.radio.num_samples, 8192)
        self.assertEqual(cfg.radio.sample_rate_hz, 20e6)

    def test_parse_set_args_types(self):
        parsed = parse_set_args([
            "radio.num_samples=8192",
            "radio.tx_repeat=2",
            "radio.tdm=false",
            "network.rl_timeout_sec=1.5",
            "actions.bw_choices_mhz=[2,4,8]",
        ])
        self.assertEqual(parsed["radio"]["num_samples"], 8192)
        self.assertIs(parsed["radio"]["tdm"], False)
        cfg = JammerConfig().with_overrides(parsed)
        self.assertEqual(cfg.actions.bw_choices_mhz, (2, 4, 8))
        self.assertAlmostEqual(cfg.network.rl_timeout_sec, 1.5)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ConfigError):
            JammerConfig.from_dict({"radio": {"nonsense": 1}})
        with self.assertRaises(ConfigError):
            JammerConfig.from_dict({"spurious_section": {}})

    def test_json_file_roundtrip_with_comments(self):
        payload = {
            "_readme": ["comments are allowed"],
            "radio": {"rx_gain_db": 25.0},
            "loop": {"frame_period_sec": 0.1},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            cfg = JammerConfig.load(path)
        self.assertEqual(cfg.radio.rx_gain_db, 25.0)
        self.assertAlmostEqual(cfg.loop.frame_period_sec, 0.1)

    def test_shipped_config_file_is_valid(self):
        path = Path(__file__).resolve().parents[1] / "config" / "jammer_arm.json"
        cfg = JammerConfig.load(path)
        self.assertFalse(cfg.tx_armed(env={}))
        self.assertEqual(cfg.radio.num_samples, 4096)


class TestValidation(unittest.TestCase):
    def test_rejects_impossible_sampling(self):
        with self.assertRaises(ConfigError):
            JammerConfig.from_dict({"radio": {"sample_rate_hz": 400e6}}).validate()
        with self.assertRaises(ConfigError):
            JammerConfig.from_dict({"radio": {"num_samples": 16}}).validate()

    def test_rejects_gain_window(self):
        with self.assertRaises(ConfigError):
            JammerConfig.from_dict({"radio": {"tx_gain_max_db": -1.0}}).validate()

    def test_rejects_channel_grid_wider_than_nyquist(self):
        with self.assertRaises(ConfigError):
            JammerConfig.from_dict({
                "radio": {"sample_rate_hz": 10e6},
                "spectrum": {"channel_width_hz": 2e6},
            }).validate()

    def test_action_coercion(self):
        cfg = JammerConfig()
        coerced = cfg.actions.coerce(99, 99.0, 7, 5, 10)
        self.assertEqual(coerced["channel_idx"], 9)
        self.assertEqual(coerced["power_db"], 20.0)
        self.assertEqual(coerced["bw_mhz"], 6)      # nearest legal choice
        self.assertEqual(coerced["waveform_mode"], 0)


if __name__ == "__main__":
    unittest.main()
