"""RFIO transport: client <-> reference server conformance.

The reference server (``jammer_arm.rfio_sim``) implements the same protocol as the
C++ daemon in ``arm_port/cpp``.  Passing these tests means the Python side of the
optional RFIO path is correct; only the UHD calls inside the daemon remain to be
verified on the board.
"""

from __future__ import annotations

import time
import unittest

import numpy as np

from jammer_arm.config import JammerConfig
from jammer_arm.core.backend import JammerBackend
from jammer_arm.radio.base import InterlockError, RadioError
from jammer_arm.radio.rfio import RfioRadio
from jammer_arm.rfio_sim import RfioSimServer

from test_protocol import free_port


class TestRfioTransport(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.server = RfioSimServer("127.0.0.1", self.port, seed=1)
        self.server.start()
        for _ in range(50):
            if self.server._server is not None:      # noqa: SLF001 - test harness
                break
            time.sleep(0.01)
        self.cfg = JammerConfig()
        self.radio = RfioRadio(self.cfg, port=self.port)

    def tearDown(self):
        try:
            self.radio.close()
        finally:
            self.server.stop()

    def test_open_configures_device(self):
        self.radio.open()
        self.assertTrue(self.radio.info.opened)
        self.assertEqual(self.radio.info.backend, "rfio")
        self.assertAlmostEqual(self.radio.info.sample_rate_hz, self.cfg.radio.sample_rate_hz)
        self.assertTrue(self.server.opened)
        self.assertEqual(int(self.server.opts["num_samples"]), self.cfg.radio.num_samples)

    def test_receive_returns_requested_samples(self):
        self.radio.open()
        samples = self.radio.receive()
        self.assertEqual(samples.size, self.cfg.radio.num_samples)
        self.assertEqual(samples.dtype, np.complex64)
        self.assertTrue(np.all(np.isfinite(samples)))
        self.assertGreater(float(np.mean(np.abs(samples) ** 2)), 0.0)
        self.assertEqual(self.server.rx_count, 1)

    def test_receive_error_surfaces_as_radio_error(self):
        self.radio.open()
        self.server.fail_rx = 1
        with self.assertRaises(RadioError):
            self.radio.receive()

    def test_transmit_requires_interlock_then_sends(self):
        self.radio.open()
        frame = np.full(self.cfg.radio.num_samples, 0.1, dtype=np.complex64)
        with self.assertRaises(InterlockError):
            self.radio.transmit(frame, 5.0, env={})
        state = self.radio.transmit(frame, 5.0, env={"ZH_RF_TX_ENABLE": "LAB_ONLY"})
        self.assertEqual(state, "running")
        self.assertEqual(len(self.server.tx_log), 1)
        self.assertEqual(self.server.tx_log[0]["nsamples"], self.cfg.radio.num_samples)
        self.assertAlmostEqual(self.server.tx_log[0]["gain"], 5.0, places=6)
        self.assertAlmostEqual(self.server.tx_log[0]["peak"], 0.1, places=5)

    def test_tx_gain_is_clamped_to_window(self):
        self.radio.open()
        frame = np.full(self.cfg.radio.num_samples, 0.1, dtype=np.complex64)
        self.radio.transmit(frame, 999.0, env={"ZH_RF_TX_ENABLE": "LAB_ONLY"})
        self.assertAlmostEqual(self.server.tx_log[-1]["gain"],
                               self.cfg.radio.tx_gain_max_db, places=6)

    def test_rebuild_paths(self):
        self.radio.open()
        self.radio.rebuild_rx()
        self.radio.rebuild()
        self.assertTrue(self.radio.info.opened)

    def test_backend_runs_over_rfio(self):
        self.radio.open()
        backend = JammerBackend(self.cfg, self.radio, logger=lambda m: None)
        reports = [backend.step() for _ in range(3)]
        self.assertTrue(all(r.rx_ok for r in reports))
        self.assertTrue(all(r.tx_state == "disabled" for r in reports))
        self.assertEqual(self.server.rx_count, 3)

    def test_open_failure_reports_actionable_error(self):
        radio = RfioRadio(self.cfg, port=free_port())
        with self.assertRaises(RadioError) as ctx:
            radio.open()
        self.assertIn("rfio daemon", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
