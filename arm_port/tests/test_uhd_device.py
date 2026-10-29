"""UHD device layer, exercised against a fake ``uhd`` module.

The board's Python bindings cannot be imported on the development host, but the
*logic* of :class:`UhdRadio` (configuration order, gain fallbacks, metadata error
mapping, TDM RX pause, rebuild paths) is independent of the driver and is what
usually breaks.  A minimal stand-in for the UHD 4.x Python API covers it here.
"""

from __future__ import annotations

import sys
import types
import unittest

import numpy as np

from jammer_arm.config import JammerConfig
from jammer_arm.radio.base import RadioError


# ── fake UHD 4.x surface ──────────────────────────────────────────────
class _FakeTypes:
    class StreamMode:
        start_cont = "start_cont"
        stop_cont = "stop_cont"
        num_samps_and_done = "num_samps_and_done"

    class StreamCMD:
        def __init__(self, mode):
            self.stream_mode = mode
            self.stream_now = False
            self.num_samps = 0

    class TuneRequest:
        def __init__(self, freq):
            self.target_freq_py = freq

    class RXMetadata:
        def __init__(self):
            self.error_code = types.SimpleNamespace(name="none")

    class TXMetadata:
        def __init__(self):
            self.start_of_burst = False
            self.end_of_burst = False


class _FakeStreamArgs:
    def __init__(self, cpu_format, otw_format):
        self.cpu_format = cpu_format
        self.otw_format = otw_format
        self.channels = []


class _FakeRxStream:
    def __init__(self, usrp):
        self.usrp = usrp
        self.cmds: list[str] = []
        self.short_read = 0

    def issue_stream_cmd(self, cmd):
        self.cmds.append(cmd.stream_mode)
        self.usrp.calls.append(("stream_cmd", cmd.stream_mode, cmd.num_samps))
        self.burst = cmd.num_samps

    def recv(self, buf, md, timeout):
        self.usrp.recv_count += 1
        code = self.usrp.next_error
        self.usrp.next_error = "none"
        md.error_code = types.SimpleNamespace(name=code)
        if code != "none":
            return 0
        n = buf.size - self.short_read
        buf[:n] = 1.0 + 1.0j
        return n


class _FakeTxStream:
    def __init__(self, usrp):
        self.usrp = usrp

    def send(self, buf, md, timeout):
        self.usrp.sent.append(np.array(buf, copy=True))
        return buf.shape[1]


class _FakeMultiUSRP:
    def __init__(self, args):
        self.args = args
        self.calls: list[tuple] = []
        self.recv_count = 0
        self.sent: list[np.ndarray] = []
        self.next_error = "none"
        self.rx_stream = _FakeRxStream(self)
        self.tx_stream = _FakeTxStream(self)
        # exercise the (gain) fallback: only the 3-argument signature is accepted
        self.gain_calls: list[tuple[str, float]] = []

    # configuration
    def set_master_clock_rate(self, rate, chan=0):
        self.calls.append(("mcr", rate))

    def set_clock_source(self, src):
        self.calls.append(("clock", src))

    def set_rx_rate(self, rate, chan=0):
        self.calls.append(("rx_rate", rate))

    def set_tx_rate(self, rate, chan=0):
        self.calls.append(("tx_rate", rate))

    def set_rx_freq(self, tune, chan=0):
        self.calls.append(("rx_freq", tune.target_freq_py))

    def set_tx_freq(self, tune, chan=0):
        self.calls.append(("tx_freq", tune.target_freq_py))

    def set_rx_gain(self, gain, name, chan):
        if not isinstance(name, str):
            raise TypeError("set_rx_gain(gain, name, chan)")
        self.gain_calls.append(("rx", gain))
        self.calls.append(("rx_gain", gain))

    def set_tx_gain(self, gain, name, chan):
        if not isinstance(name, str):
            raise TypeError("set_tx_gain(gain, name, chan)")
        self.gain_calls.append(("tx", gain))
        self.calls.append(("tx_gain", gain))

    def set_rx_antenna(self, ant, chan=0):
        self.calls.append(("rx_ant", ant))

    def set_tx_antenna(self, ant, chan=0):
        self.calls.append(("tx_ant", ant))

    # streams
    def get_rx_stream(self, args):
        self.calls.append(("get_rx_stream", args.cpu_format))
        return self.rx_stream

    def get_tx_stream(self, args):
        self.calls.append(("get_tx_stream", args.cpu_format))
        return self.tx_stream

    # diagnostics
    def get_rx_rate(self, chan=0):
        return 20e6

    def get_rx_freq(self, chan=0):
        return 3e9

    def get_rx_gain(self, name="", chan=0):
        return 15.0

    def get_tx_gain(self, name="", chan=0):
        return 0.0

    def get_rx_antenna(self, chan=0):
        return "TX/RX"

    def get_tx_antenna(self, chan=0):
        return "TX/RX"

    def get_mboard_name(self, chan=0):
        return "X310 (fake)"

    def get_rx_subdev_name(self, chan=0):
        return "A:0"

    def get_master_clock_rate(self, chan=0):
        return 200e6

    def get_tx_rate(self, chan=0):
        return 20e6


def install_fake_uhd() -> types.ModuleType:
    mod = types.ModuleType("uhd")
    usrp = types.ModuleType("uhd.usrp")
    usrp.MultiUSRP = _FakeMultiUSRP
    usrp.StreamArgs = _FakeStreamArgs
    types_mod = types.ModuleType("uhd.types")
    types_mod.StreamMode = _FakeTypes.StreamMode
    types_mod.StreamCMD = _FakeTypes.StreamCMD
    types_mod.TuneRequest = _FakeTypes.TuneRequest
    types_mod.RXMetadata = _FakeTypes.RXMetadata
    types_mod.TXMetadata = _FakeTypes.TXMetadata
    mod.usrp = usrp
    mod.types = types_mod
    mod.get_version_string = lambda: "UHD 4.9.0.0-fake"
    sys.modules["uhd"] = mod
    sys.modules["uhd.usrp"] = usrp
    sys.modules["uhd.types"] = types_mod
    return mod


class TestUhdRadio(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("uhd")
        self.fake = install_fake_uhd()
        from jammer_arm.radio.uhd_device import UhdRadio
        self.cfg = JammerConfig.load(None, {"radio.settle_sec": 0.0})
        self.radio = UhdRadio(self.cfg)
        self.radio.open()
        self.usrp = self.radio._usrp  # noqa: SLF001 - test introspection

    def tearDown(self):
        try:
            self.radio.close()
        finally:
            if self._saved is None:
                for key in ("uhd", "uhd.usrp", "uhd.types"):
                    sys.modules.pop(key, None)
            else:
                sys.modules["uhd"] = self._saved

    def test_configuration_matches_matlab_settings(self):
        calls = self.usrp.calls
        self.assertIn(("mcr", 200e6), calls)
        self.assertIn(("clock", "internal"), calls)
        self.assertIn(("rx_rate", 20e6), calls)
        self.assertIn(("tx_rate", 20e6), calls)
        self.assertIn(("rx_freq", 3e9), calls)
        self.assertIn(("tx_freq", 3e9), calls)
        self.assertIn(("rx_ant", "TX/RX"), calls)
        self.assertIn(("tx_ant", "TX/RX"), calls)
        self.assertIn(("rx_gain", 15.0), calls)
        self.assertIn(("get_rx_stream", "fc32"), calls)
        self.assertIn(("get_tx_stream", "fc32"), calls)

    def test_device_args_come_from_config(self):
        self.assertEqual(self.usrp.args, self.cfg.radio.device_args)

    def test_info_reports_actuals(self):
        self.assertTrue(self.radio.info.opened)
        self.assertAlmostEqual(self.radio.info.sample_rate_hz, 20e6)
        self.assertAlmostEqual(self.radio.info.center_freq_hz, 3e9)
        self.assertEqual(self.radio.info.details.get("mboard"), "X310 (fake)")

    def test_receive_returns_buffer(self):
        samples = self.radio.receive()
        self.assertEqual(samples.size, self.cfg.radio.num_samples)
        self.assertEqual(self.usrp.recv_count, 1)

    def test_burst_mode_requests_one_frame(self):
        """NUM_SAMPS_AND_DONE is what keeps a 12 fps loop from overflowing."""
        self.usrp.rx_stream.cmds.clear()
        self.radio.receive()
        self.assertIn("num_samps_and_done", self.usrp.rx_stream.cmds)
        burst = [c for c in self.usrp.calls if c[0] == "stream_cmd"][-1]
        self.assertEqual(burst[2], self.cfg.radio.num_samples)

    def test_continuous_mode_starts_stream(self):
        cfg = JammerConfig.load(None, {"radio.settle_sec": 0.0,
                                       "radio.rx_mode": "continuous"})
        from jammer_arm.radio.uhd_device import UhdRadio
        radio = UhdRadio(cfg)
        radio.open()
        usrp = radio._usrp  # noqa: SLF001
        self.assertIn("start_cont", usrp.rx_stream.cmds)
        usrp.rx_stream.cmds.clear()
        samples = radio.receive()
        self.assertEqual(samples.size, cfg.radio.num_samples)
        self.assertNotIn("num_samps_and_done", usrp.rx_stream.cmds)
        radio.close()

    def test_timeout_metadata_raises(self):
        self.usrp.next_error = "timeout"
        with self.assertRaises(RadioError) as ctx:
            self.radio.receive()
        self.assertIn("timeout", str(ctx.exception))

    def test_overflow_counts_and_raises(self):
        self.usrp.next_error = "overflow"
        with self.assertRaises(RadioError):
            self.radio.receive()
        self.assertEqual(self.radio.info.rx_overruns, 1)

    def test_short_receive_raises(self):
        self.usrp.rx_stream.short_read = 10
        with self.assertRaises(RadioError):
            self.radio.receive()

    def test_transmit_does_not_touch_burst_rx(self):
        """In burst mode RX is already idle, so TX must not issue stream commands."""
        frame = np.full(self.cfg.radio.num_samples, 0.1, dtype=np.complex64)
        self.usrp.rx_stream.cmds.clear()
        self.radio.transmit(frame, 4.0, env={"ZH_RF_TX_ENABLE": "LAB_ONLY"})
        self.assertEqual(self.usrp.rx_stream.cmds, [])
        self.assertEqual(len(self.usrp.sent), 1)
        self.assertEqual(self.usrp.sent[0].shape[1], self.cfg.radio.num_samples)

    def test_transmit_pauses_continuous_rx(self):
        cfg = JammerConfig.load(None, {"radio.settle_sec": 0.0,
                                       "radio.rx_mode": "continuous",
                                       "radio.tdm": True})
        from jammer_arm.radio.uhd_device import UhdRadio
        radio = UhdRadio(cfg)
        radio.open()
        usrp = radio._usrp  # noqa: SLF001
        usrp.rx_stream.cmds.clear()
        radio.transmit(np.full(cfg.radio.num_samples, 0.1, dtype=np.complex64), 4.0,
                       env={"ZH_RF_TX_ENABLE": "LAB_ONLY"})
        self.assertEqual(usrp.rx_stream.cmds, ["stop_cont", "start_cont"])
        self.assertEqual(len(usrp.sent), 1)
        radio.close()

    def test_transmit_clamps_gain(self):
        self.radio.transmit(np.zeros(self.cfg.radio.num_samples, dtype=np.complex64), 500.0,
                            env={"ZH_RF_TX_ENABLE": "LAB_ONLY"})
        self.assertEqual(self.usrp.gain_calls[-1], ("tx", self.cfg.radio.tx_gain_max_db))

    def test_transmit_respects_tx_repeat(self):
        cfg = JammerConfig.load(None, {"radio.tx_repeat": 3, "radio.settle_sec": 0.0})
        from jammer_arm.radio.uhd_device import UhdRadio
        radio = UhdRadio(cfg)
        radio.open()
        usrp = radio._usrp  # noqa: SLF001
        radio.transmit(np.zeros(cfg.radio.num_samples, dtype=np.complex64), 0.0,
                       env={"ZH_RF_TX_ENABLE": "LAB_ONLY"})
        self.assertEqual(len(usrp.sent), 3)
        radio.close()

    def test_rebuild_rx_recreates_stream(self):
        before = sum(1 for c in self.usrp.calls if c[0] == "get_rx_stream")
        self.radio.rebuild_rx()
        after = sum(1 for c in self.usrp.calls if c[0] == "get_rx_stream")
        self.assertEqual(after, before + 1, "rebuild_rx must request a new RX streamer")
        self.assertTrue(self.radio.info.opened)
        self.assertEqual(self.radio.receive().size, self.cfg.radio.num_samples)

    def test_close_is_idempotent(self):
        self.radio.close()
        self.radio.close()
        self.assertFalse(self.radio.info.opened)

    def test_missing_bindings_produce_actionable_error(self):
        sys.modules["uhd"] = None
        from jammer_arm.radio.uhd_device import UhdRadio
        radio = UhdRadio(self.cfg)
        with self.assertRaises(RadioError) as ctx:
            radio.open()
        message = str(ctx.exception)
        self.assertIn("ENABLE_PYTHON_API", message)
        self.assertIn("rfio", message)

    def test_unreachable_device_reports_args(self):
        class Boom(_FakeMultiUSRP):
            def __init__(self, args):
                raise RuntimeError("no devices found")

        self.fake.usrp.MultiUSRP = Boom
        from jammer_arm.radio.uhd_device import UhdRadio
        radio = UhdRadio(self.cfg)
        with self.assertRaises(RadioError) as ctx:
            radio.open()
        self.assertIn("no devices found", str(ctx.exception))
        self.assertIn("uhd_find_devices", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
