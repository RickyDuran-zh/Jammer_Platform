"""Wire protocol compatibility with the existing UI and RL service."""

from __future__ import annotations

import json
import socket
import unittest

import numpy as np

from jammer_arm.config import JammerConfig
from jammer_arm.dsp.sensing import SensingResult
from jammer_arm.net.command import CommandClient
from jammer_arm.net.jsonline import JsonLineConnection, LineServer, LineTimeout
from jammer_arm.net.rl_client import RLClient
from jammer_arm.net.telemetry import TelemetryClient


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def cfg_with_ports(rl=None, telem=None, cmd=None) -> JammerConfig:
    overrides = {}
    if rl:
        overrides["network.rl_port"] = rl
    if telem:
        overrides["network.telemetry_port"] = telem
    if cmd:
        overrides["network.command_port"] = cmd
    return JammerConfig().with_overrides(overrides)


class TestJsonLine(unittest.TestCase):
    def test_roundtrip(self):
        port = free_port()
        server = LineServer("127.0.0.1", port,
                            lambda pkt: {"type": "rsp", "n": len(pkt)}).start()
        try:
            conn = JsonLineConnection("127.0.0.1", port, timeout=1.0, name="t")
            self.assertTrue(conn.connect())
            self.assertEqual(conn.request({"a": 1})["n"], 1)
            self.assertEqual(conn.request({"a": 1, "b": 2})["n"], 2)
            conn.close()
        finally:
            server.stop()

    def test_timeout_raises(self):
        port = free_port()
        server = LineServer("127.0.0.1", port, lambda pkt: None).start()
        try:
            conn = JsonLineConnection("127.0.0.1", port, timeout=0.2, name="t")
            self.assertTrue(conn.connect())
            with self.assertRaises(LineTimeout):
                conn.request({"a": 1})
            conn.close()
        finally:
            server.stop()

    def test_reconnect_after_peer_restart(self):
        port = free_port()
        server = LineServer("127.0.0.1", port, lambda pkt: {"type": "rsp"}).start()
        conn = JsonLineConnection("127.0.0.1", port, timeout=1.0, name="t")
        self.assertTrue(conn.connect())
        conn.request({"a": 1})
        server.stop()
        conn.close()
        server2 = LineServer("127.0.0.1", port, lambda pkt: {"type": "rsp"}).start()
        try:
            self.assertTrue(conn.connect())
            self.assertEqual(conn.request({"a": 2})["type"], "rsp")
        finally:
            conn.close()
            server2.stop()


class TestRLClient(unittest.TestCase):
    def test_request_action_packet_shape(self):
        port = free_port()
        seen: list[dict] = []

        def handler(pkt):
            seen.append(pkt)
            return {
                "type": "action_rsp", "status": "ok",
                "action": {"channel_idx": 7, "power_db": 12.5, "bw_mhz": 6, "waveform_mode": 1},
                "rl_meta": {"policy": "ppo", "value": 0.5, "latency_ms": 3},
            }

        server = LineServer("127.0.0.1", port, handler).start()
        try:
            cfg = cfg_with_ports(rl=port)
            client = RLClient(cfg)
            feats = np.zeros((10, 4))
            prev = {"channel_idx": 1, "power_db": 5.0, "bw_mhz": 2, "waveform_mode": 0}
            action, meta = client.request_action(11, feats, prev, "running")
        finally:
            server.stop()

        self.assertEqual(action, {"channel_idx": 7, "power_db": 12.5,
                                  "bw_mhz": 6, "waveform_mode": 1})
        self.assertEqual(meta["policy"], "ppo")

        pkt = seen[0]
        self.assertEqual(pkt["type"], "state_req")
        self.assertEqual(pkt["version"], "2.0")
        self.assertEqual(pkt["seq"], 11)
        self.assertEqual(pkt["session_id"], cfg.network.session_id)
        self.assertEqual(pkt["state"]["task"], "jammer_spectrum_ctrl")
        self.assertEqual(pkt["state"]["num_channels"], 10)
        self.assertEqual(len(pkt["state"]["channel_features"]), 10)
        self.assertEqual(pkt["state"]["prev_action"]["channel_idx"], 1)
        self.assertEqual(pkt["state"]["tx_state"], "running")
        self.assertIsInstance(pkt["timestamp_ms"], int)

    def test_out_of_range_action_is_coerced(self):
        port = free_port()
        server = LineServer("127.0.0.1", port, lambda pkt: {
            "status": "ok",
            "action": {"channel_idx": 42, "power_db": 99, "bw_mhz": 5, "waveform_mode": 9},
            "rl_meta": {"policy": "a2c", "latency_ms": 1},
        }).start()
        try:
            client = RLClient(cfg_with_ports(rl=port))
            prev = {"channel_idx": 0, "power_db": 0.0, "bw_mhz": 2, "waveform_mode": 0}
            action, _ = client.request_action(1, np.zeros((10, 4)), prev, "running")
        finally:
            server.stop()
        self.assertEqual(action["channel_idx"], 9)
        self.assertEqual(action["power_db"], 20.0)
        self.assertIn(action["bw_mhz"], (2, 4, 6, 8))
        self.assertIn(action["waveform_mode"], (0, 1))

    def test_missing_service_falls_back_to_previous_action(self):
        cfg = cfg_with_ports(rl=free_port())
        client = RLClient(cfg)
        prev = {"channel_idx": 3, "power_db": 10.0, "bw_mhz": 4, "waveform_mode": 0}
        action, meta = client.request_action(1, np.zeros((10, 4)), prev, "running")
        self.assertEqual(action, prev)
        self.assertEqual(meta["policy"], "fallback")

    def test_bad_response_falls_back(self):
        port = free_port()
        server = LineServer("127.0.0.1", port, lambda pkt: {"status": "error"}).start()
        try:
            client = RLClient(cfg_with_ports(rl=port))
            prev = {"channel_idx": 2, "power_db": 5.0, "bw_mhz": 2, "waveform_mode": 0}
            action, meta = client.request_action(1, np.zeros((10, 4)), prev, "running")
        finally:
            server.stop()
        self.assertEqual(action, prev)
        self.assertEqual(meta["policy"], "fallback")


class TestTelemetry(unittest.TestCase):
    def _sensing(self, cfg) -> SensingResult:
        n = cfg.radio.num_samples
        return SensingResult(
            channel_features=np.full((10, 4), 0.5),
            channel_map=np.arange(10) % 4,
            snr_est=12.0, bw_est=8e6, sig_type=1,
            spectrum_abs=np.linspace(0, 1, n),
            freq_axis_hz=np.linspace(2.99e9, 3.01e9, n),
        )

    def test_packet_matches_ui_contract(self):
        cfg = JammerConfig()
        client = TelemetryClient(cfg)
        sensing = self._sensing(cfg)
        action = {"channel_idx": 4, "power_db": 10.0, "bw_mhz": 4, "waveform_mode": 0}
        jam = np.ones(cfg.radio.num_samples, dtype=np.complex64) * 0.1
        pkt = client.build_packet(7, sensing, jam, "running", action,
                                  {"policy": "ppo", "latency_ms": 2},
                                  hw_actuals={"bw_mhz": 4.0, "tx_gain_db": 5.0,
                                              "requested_bw_mhz": 4.0, "clipped": False})

        self.assertEqual(pkt["type"], "telemetry")
        self.assertEqual(pkt["version"], "2.0")
        self.assertEqual(pkt["seq"], 7)
        self.assertEqual(pkt["session_id"], cfg.network.session_id)
        tel = pkt["telemetry"]
        for key in ("snr_est", "bw_est", "sig_type", "tx_state", "spectrum",
                    "freq_axis_ghz", "jam_waveform_abs", "channel_map", "channel_features"):
            self.assertIn(key, tel)
        self.assertEqual(len(tel["spectrum"]), len(tel["freq_axis_ghz"]))
        self.assertLessEqual(len(tel["spectrum"]), 512 + 1)
        self.assertLessEqual(len(tel["jam_waveform_abs"]), 512 + 1)
        self.assertEqual(len(tel["channel_map"]), 10)
        self.assertEqual(len(tel["channel_features"]), 10)
        self.assertAlmostEqual(tel["freq_axis_ghz"][0], sensing.freq_axis_hz[0] / 1e9, places=9)
        self.assertEqual(pkt["action"]["channel_idx"], 4)
        self.assertEqual(pkt["rl_meta"]["policy"], "ppo")
        self.assertTrue(tel["hw_actuals"]["requested_bw_mhz"] == 4.0)
        self.assertAlmostEqual(tel["hw_actuals"]["bw_mhz"], 4.0, places=6)
        json.dumps(pkt)   # must be encodable

    def test_hw_actuals_can_be_disabled(self):
        cfg = JammerConfig().with_overrides({"emit_hw_actuals": False})
        client = TelemetryClient(cfg)
        pkt = client.build_packet(1, self._sensing(cfg),
                                  np.zeros(cfg.radio.num_samples, dtype=np.complex64),
                                  "disabled", {"channel_idx": 0, "power_db": 0,
                                               "bw_mhz": 2, "waveform_mode": 0},
                                  {"policy": "agent"})
        self.assertNotIn("hw_actuals", pkt["telemetry"])

    def test_publish_to_listener(self):
        port = free_port()
        collected: list[dict] = []
        server = LineServer("127.0.0.1", port, lambda pkt: collected.append(pkt)).start()
        try:
            cfg = cfg_with_ports(telem=port)
            client = TelemetryClient(cfg)
            self.assertTrue(client.connect())
            pkt = client.build_packet(1, self._sensing(cfg),
                                      np.zeros(cfg.radio.num_samples, dtype=np.complex64),
                                      "disabled", {"channel_idx": 0, "power_db": 0,
                                                   "bw_mhz": 2, "waveform_mode": 0},
                                      {"policy": "agent"})
            self.assertTrue(client.publish(pkt))
            import time
            for _ in range(50):
                if collected:
                    break
                time.sleep(0.02)
            client.close()
        finally:
            server.stop()
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["type"], "telemetry")


class TestCommandClient(unittest.TestCase):
    def test_poll_returns_commands(self):
        port = free_port()
        state = {"cmds": [{"type": "set_control_mode", "mode": "agent"},
                          {"channel_idx": 5}]}

        def handler(pkt):
            if pkt.get("type") != "cmd_poll":
                return None
            cmds, state["cmds"] = state["cmds"], []
            return {"type": "cmd_rsp", "commands": cmds, "timestamp_ms": 0}

        server = LineServer("127.0.0.1", port, handler).start()
        try:
            client = CommandClient(cfg_with_ports(cmd=port))
            cmds = client.poll()
            self.assertEqual(len(cmds), 2)
            self.assertEqual(cmds[0]["mode"], "agent")
            self.assertEqual(client.poll(), [])
            client.close()
        finally:
            server.stop()

    def test_poll_without_ui_returns_empty(self):
        client = CommandClient(cfg_with_ports(cmd=free_port()))
        self.assertEqual(client.poll(), [])
        self.assertFalse(client.connected)


if __name__ == "__main__":
    unittest.main()
