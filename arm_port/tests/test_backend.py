"""Frame loop behaviour: control ownership, RF interlock, recovery, telemetry."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from jammer_arm.config import JammerConfig
from jammer_arm.core.backend import JammerBackend
from jammer_arm.core.mock_backend import MockBackend
from jammer_arm.net.command import CommandClient
from jammer_arm.net.jsonline import LineServer
from jammer_arm.net.rl_client import RLClient
from jammer_arm.net.telemetry import TelemetryClient
from jammer_arm.radio.mock import MockRadio

from test_protocol import cfg_with_ports, free_port


def _noop(_msg: str) -> None:
    pass


class TestMockBackend(unittest.TestCase):
    def test_runs_requested_frames(self):
        cfg = JammerConfig()
        backend = MockBackend(cfg, seed=5, logger=_noop)
        reports = []
        status = backend.run(max_frames=3, on_frame=reports.append)
        self.assertEqual(len(reports), 3)
        self.assertEqual(status["metrics"]["frames"], 3)
        self.assertTrue(all(r.tx_state == "mock_running" for r in reports))
        self.assertTrue(all(r.sensing.channel_features.shape == (10, 4) for r in reports))
        self.assertEqual([r.seq for r in reports], [1, 2, 3])

    def test_duration_is_respected(self):
        cfg = JammerConfig().with_overrides({"loop.mock_frame_period_sec": 0.02})
        backend = MockBackend(cfg, seed=6, logger=_noop)
        t0 = time.monotonic()
        status = backend.run(duration_sec=0.15)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        self.assertGreaterEqual(status["metrics"]["frames"], 3)


class TestControlOwnership(unittest.TestCase):
    def test_agent_mode_holds_action_and_ignores_rl(self):
        cfg = JammerConfig()
        backend = MockBackend(cfg, seed=1, logger=_noop)
        backend.state.control.agent_control_mode = True
        backend.state.prev_action = {"channel_idx": 6, "power_db": 15.0,
                                     "bw_mhz": 6, "waveform_mode": 1}
        report = backend.step()
        self.assertEqual(report.control, "agent")
        self.assertEqual(report.rl_meta["policy"], "agent")
        self.assertEqual(report.action["channel_idx"], 6)
        self.assertEqual(report.action["bw_mhz"], 6)

    def test_command_bridge_switches_mode_and_applies_action(self):
        port = free_port()
        pending = [[{"type": "set_control_mode", "mode": "agent"},
                    {"channel_idx": 8, "power_db": 12.0, "bw_mhz": 8, "waveform_mode": 1}]]

        def handler(pkt):
            if pkt.get("type") != "cmd_poll":
                return None
            cmds = pending.pop(0) if pending else []
            return {"type": "cmd_rsp", "commands": cmds}

        server = LineServer("127.0.0.1", port, handler).start()
        try:
            cfg = cfg_with_ports(cmd=port)
            backend = MockBackend(cfg, seed=2, logger=_noop)
            backend.commands = CommandClient(cfg)
            report = backend.step()
            self.assertTrue(backend.state.control.agent_control_mode)
            self.assertEqual(report.control, "agent")
            self.assertEqual(report.action["channel_idx"], 8)
            self.assertEqual(report.action["bw_mhz"], 8)
            self.assertEqual(report.action["waveform_mode"], 1)
            # the following poll returns nothing and the action is held
            report2 = backend.step()
            self.assertEqual(report2.action["channel_idx"], 8)
            backend.close()
        finally:
            server.stop()

    def test_invalid_commands_are_ignored(self):
        port = free_port()
        pending = [[{"bw_mhz": 5}, {"waveform_mode": 7}, {"power_db": 999}]]

        def handler(pkt):
            if pkt.get("type") != "cmd_poll":
                return None
            return {"type": "cmd_rsp", "commands": pending.pop(0) if pending else []}

        server = LineServer("127.0.0.1", port, handler).start()
        try:
            cfg = cfg_with_ports(cmd=port)
            backend = MockBackend(cfg, seed=3, logger=_noop)
            backend.commands = CommandClient(cfg)
            backend.state.prev_action = {"channel_idx": 2, "power_db": 5.0,
                                         "bw_mhz": 4, "waveform_mode": 0}
            report = backend.step()
            self.assertEqual(report.action["bw_mhz"], 4)
            self.assertEqual(report.action["waveform_mode"], 0)
            self.assertEqual(report.action["power_db"], 20.0)   # clamped, not rejected
            backend.close()
        finally:
            server.stop()


class TestInterlockAndTx(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ZH_RF_TX_ENABLE", None)

    def tearDown(self):
        os.environ.pop("ZH_RF_TX_ENABLE", None)

    def test_disarmed_never_transmits(self):
        cfg = JammerConfig()
        radio = MockRadio(cfg, seed=4)
        radio.open()
        backend = JammerBackend(cfg, radio, logger=_noop)
        reports = [backend.step() for _ in range(3)]
        self.assertTrue(all(r.tx_state == "disabled" for r in reports))
        self.assertEqual(radio.tx_frames, [])
        self.assertEqual(backend.metrics["interlock_blocks"], 3)

    def test_armed_transmits_normalised_frames(self):
        os.environ["ZH_RF_TX_ENABLE"] = "LAB_ONLY"
        cfg = JammerConfig()
        radio = MockRadio(cfg, seed=4)
        radio.open()
        backend = JammerBackend(cfg, radio, logger=_noop)
        backend.state.prev_action = {"channel_idx": 3, "power_db": 20.0,
                                     "bw_mhz": 4, "waveform_mode": 0}
        report = backend.step()
        self.assertEqual(report.tx_state, "running")
        self.assertEqual(len(radio.tx_frames), 1)
        frame = radio.tx_frames[0]
        self.assertAlmostEqual(frame["peak"], cfg.radio.tx_baseband_peak, places=6)
        self.assertLessEqual(frame["gain_db"], cfg.radio.tx_gain_max_db)
        self.assertEqual(backend.metrics["tx_frames"], 1)

    def test_oversized_frame_is_rejected_by_the_device(self):
        cfg = JammerConfig()
        radio = MockRadio(cfg)
        radio.open()
        bad = np.full(cfg.radio.num_samples, 0.9, dtype=np.complex64)
        from jammer_arm.radio.base import TxFrameError
        with self.assertRaises(TxFrameError):
            radio.transmit(bad, 0.0, env={"ZH_RF_TX_ENABLE": "LAB_ONLY"})


class TestLoopIsolation(unittest.TestCase):
    def test_unexpected_frame_error_does_not_stop_the_loop(self):
        """Anything outside the RadioError ladder is contained per frame."""

        class ExplodingRadio(MockRadio):
            def __init__(self, cfg, seed=None):
                super().__init__(cfg, seed=seed)
                self.remaining = 2

            def _receive(self, num_samples):
                if self.remaining > 0:
                    self.remaining -= 1
                    raise ValueError("simulated DSP/transport bug")
                return super()._receive(num_samples)

        cfg = JammerConfig()
        radio = ExplodingRadio(cfg, seed=12)
        radio.open()
        backend = JammerBackend(cfg, radio, logger=_noop)
        reports = []
        status = backend.run(max_frames=5, on_frame=reports.append)
        self.assertEqual(status["metrics"]["frame_errors"], 2)
        self.assertEqual(len(reports), 3)          # 5 attempts, 3 good frames
        self.assertGreaterEqual(status["metrics"]["frames"], 3)


class TestRecovery(unittest.TestCase):
    def test_rx_failure_keeps_loop_and_cache(self):
        cfg = JammerConfig()
        radio = MockRadio(cfg, seed=7)
        radio.open()
        backend = JammerBackend(cfg, radio, logger=_noop)
        backend.step()
        cached = backend.state.last_sensing
        radio.fail_always(True)
        for _ in range(cfg.loop.rx_timeout_reinit_threshold + 1):
            report = backend.step()
            self.assertFalse(report.rx_ok)
            self.assertIsNotNone(report.sensing)
            self.assertGreaterEqual(report.sensing.channel_features.shape[0], 10)
        self.assertGreaterEqual(backend.metrics["rx_failures"],
                                cfg.loop.rx_timeout_reinit_threshold)
        self.assertGreaterEqual(
            backend.metrics["rx_rebuilds"] + backend.metrics["radio_rebuilds"], 1)
        radio.fail_always(False)
        self.assertTrue(backend.step().rx_ok)
        self.assertIsNotNone(cached)


class TestWarmup(unittest.TestCase):
    """Warm-up frames must not pollute the stability gate."""

    def test_warmup_resets_quality_counters(self):
        cfg = JammerConfig()
        radio = MockRadio(cfg, seed=13)
        radio.open()
        radio.fail_next(1)                      # a cold-start RX hiccup
        backend = JammerBackend(cfg, radio, logger=_noop, warmup_frames=2)
        status = backend.run(max_frames=5)
        self.assertEqual(status["metrics"]["warmup_frames"], 2)
        self.assertEqual(status["metrics"]["rx_failures"], 0,
                         "counters from the warm-up window must be discarded")
        # an RX failure still produces a frame (cached sensing), so all 5 count
        self.assertEqual(status["metrics"]["frames"], 5)

    def test_no_warmup_counts_everything(self):
        cfg = JammerConfig()
        radio = MockRadio(cfg, seed=14)
        radio.open()
        radio.fail_next(1)
        backend = JammerBackend(cfg, radio, logger=_noop)
        status = backend.run(max_frames=4)
        self.assertGreaterEqual(status["metrics"]["rx_failures"], 1)

    def test_assess_ignores_warmup_frames_implicitly(self):
        """A warm-up spike in max_frame_ms is gone before the snapshot is written."""
        cfg = JammerConfig()
        backend = MockBackend(cfg, seed=15, logger=_noop, warmup_frames=1)
        status = backend.run(max_frames=3)
        self.assertLess(status["metrics"]["max_frame_ms"], 1000.0)


class TestDecisionAndTelemetry(unittest.TestCase):
    def test_rl_action_is_executed(self):
        port = free_port()
        server = LineServer("127.0.0.1", port, lambda pkt: {
            "status": "ok",
            "action": {"channel_idx": 7, "power_db": 15.0, "bw_mhz": 6, "waveform_mode": 1},
            "rl_meta": {"policy": "ppo", "value": 0.1, "latency_ms": 1},
        }).start()
        try:
            cfg = cfg_with_ports(rl=port)
            backend = MockBackend(cfg, seed=8, logger=_noop)
            backend.rl = RLClient(cfg)
            report = backend.step()
        finally:
            server.stop()
        self.assertEqual(report.rl_meta["policy"], "ppo")
        self.assertEqual(report.action["channel_idx"], 7)
        self.assertEqual(report.action["waveform_mode"], 1)
        self.assertEqual(backend.state.prev_action["channel_idx"], 7)

    def test_telemetry_reaches_ui_with_schema(self):
        port = free_port()
        collected: list[dict] = []
        server = LineServer("127.0.0.1", port, lambda pkt: collected.append(pkt)).start()
        try:
            cfg = cfg_with_ports(telem=port)
            backend = MockBackend(cfg, seed=9, logger=_noop)
            backend.telemetry = TelemetryClient(cfg)
            backend.step()
            backend.step()
            for _ in range(100):
                if len(collected) >= 2:
                    break
                time.sleep(0.02)
            backend.close()
        finally:
            server.stop()
        self.assertEqual(len(collected), 2)
        pkt = collected[-1]
        self.assertEqual(pkt["type"], "telemetry")
        self.assertEqual(pkt["seq"], 2)
        self.assertEqual(len(pkt["telemetry"]["channel_map"]), 10)
        self.assertEqual(len(pkt["telemetry"]["spectrum"]),
                         len(pkt["telemetry"]["freq_axis_ghz"]))
        self.assertEqual(pkt["telemetry"]["tx_state"], "mock_running")
        self.assertIn("executed_action", pkt["telemetry"])
        self.assertEqual(pkt["action"], pkt["telemetry"]["executed_action"])

    def test_status_snapshot_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            cfg = JammerConfig().with_overrides({"loop.status_write_interval_sec": 0.0})
            backend = MockBackend(cfg, seed=10, logger=_noop, status_path=path)
            backend.run(max_frames=2)
            self.assertTrue(path.is_file())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["mode"], "mock")
            self.assertEqual(data["seq"], 2)
            self.assertIn("metrics", data)


if __name__ == "__main__":
    unittest.main()
