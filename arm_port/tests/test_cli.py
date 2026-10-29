"""Command line surface: exit codes, JSON output, and graceful device failures.

These tests spawn real subprocesses (not in-process calls) because the CLI installs
signal handlers and owns process exit codes - exactly the parts a board operator
relies on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ARM_DIR = Path(__file__).resolve().parents[1]
PKG_PARENT = ARM_DIR / "python"
DEFAULT_CONFIG = ARM_DIR / "config" / "jammer_arm.json"


def run_cli(*args: str, timeout: float = 90.0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PKG_PARENT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.pop("ZH_RF_TX_ENABLE", None)
    return subprocess.run(
        [sys.executable, "-m", "jammer_arm", *args],
        capture_output=True, text=True, timeout=timeout, env=env, cwd=str(ARM_DIR),
    )


class TestConfigCommands(unittest.TestCase):
    def test_show_config_emits_json(self):
        proc = run_cli("show-config", "--config", str(DEFAULT_CONFIG))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["spectrum"]["num_channels"], 10)
        self.assertAlmostEqual(data["radio"]["sample_rate_hz"], 20e6)
        self.assertFalse(data["tx_armed_env"])
        self.assertIn("channel_centers_hz", data)

    def test_bad_override_is_rejected_with_exit_3(self):
        proc = run_cli("show-config", "--set", "radio.nonsense=1")
        self.assertEqual(proc.returncode, 3, proc.stdout)
        self.assertIn("configuration error", proc.stderr)

    def test_interlock_key_rejected(self):
        proc = run_cli("show-config", "--set", "tx_enabled=true")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("zh_rf_tx_enable", proc.stderr.lower())

    def test_override_changes_value(self):
        proc = run_cli("show-config", "--set", "radio.num_samples=8192")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["radio"]["num_samples"], 8192)


class TestDiagnostics(unittest.TestCase):
    def test_doctor_always_reports_interlock_state(self):
        proc = run_cli("doctor")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rf interlock", proc.stdout)
        self.assertIn("disarmed", proc.stdout)
        self.assertIn("checks,", proc.stdout)

    def test_selftest_passes(self):
        proc = run_cli("selftest")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("failure(s)", proc.stdout)
        self.assertNotIn("[FAIL]", proc.stdout)

    def test_bench_prints_budget(self):
        proc = run_cli("bench", "--iterations", "20")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("frame budget", proc.stdout)
        self.assertIn("sensing", proc.stdout)


class TestRunCommands(unittest.TestCase):
    def test_mock_backend_runs_and_exits_cleanly(self):
        proc = run_cli("mock", "--frames", "3", "--no-rl", "--no-telemetry", "--no-cmd",
                       "--quiet")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("frames          : 3", proc.stdout)
        self.assertIn("tx_armed=no", proc.stdout)

    def test_warmup_flag_reaches_the_snapshot(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            proc = run_cli("mock", "--frames", "5", "--warmup", "2", "--quiet",
                           "--no-rl", "--no-telemetry", "--no-cmd",
                           "--status", str(path))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["metrics"]["warmup_frames"], 2)

    def test_agent_control_mode_flag(self):
        proc = run_cli("mock", "--frames", "2", "--control", "agent", "--quiet",
                       "--no-rl", "--no-telemetry", "--no-cmd")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_rfio_daemon_fails_with_actionable_message(self):
        proc = run_cli("run", "--radio", "rfio", "--rfio-port", "1", "--frames", "1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("rfio daemon", proc.stderr)

    def test_status_snapshot_is_written(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            proc = run_cli("mock", "--frames", "3", "--quiet", "--no-rl",
                           "--no-telemetry", "--no-cmd", "--status", str(path),
                           "--set", "loop.status_write_interval_sec=0")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(path.is_file())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["mode"], "mock")
            self.assertFalse(data["tx_armed"])
            self.assertEqual(data["metrics"]["tx_frames"], 0)

    def test_help_and_version(self):
        for flag in ("--help", "--version"):
            with self.subTest(flag=flag):
                proc = run_cli(flag)
                self.assertEqual(proc.returncode, 0)
                self.assertTrue(proc.stdout.strip())

    def test_unknown_command_exits_2(self):
        proc = run_cli("nonsense")
        self.assertEqual(proc.returncode, 2)


class TestAssessGate(unittest.TestCase):
    """Gate G4: the board run must be judged by code, not by reading logs."""

    def _snapshot(self, tmp: Path, **metrics) -> Path:
        base = {
            "frames": 600, "rx_failures": 0, "overruns": 0, "frame_errors": 0,
            "max_frame_ms": 31.0, "tx_frames": 0, "tx_errors": 0,
        }
        base.update(metrics)
        payload = {
            "mode": "live",
            "frame_period_ms": 80.0,
            "tx_armed": False,
            "metrics": base,
        }
        path = tmp / "status.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_healthy_snapshot_passes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snapshot(Path(tmp))
            proc = run_cli("assess", "--status", str(path), "--min-frames", "600")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ALL 7 CRITERIA PASSED", proc.stdout)

    def test_overruns_fail_the_gate(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snapshot(Path(tmp), overruns=3)
            proc = run_cli("assess", "--status", str(path))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no rx overruns", proc.stdout)
        self.assertIn("FAILED", proc.stdout)

    def test_too_few_frames_fail(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snapshot(Path(tmp), frames=12)
            proc = run_cli("assess", "--status", str(path), "--min-frames", "600")
        self.assertEqual(proc.returncode, 1)

    def test_slow_frames_fail(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snapshot(Path(tmp), max_frame_ms=500.0)
            proc = run_cli("assess", "--status", str(path))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("frame time within budget", proc.stdout)

    def test_interlock_consistency_detected(self):
        """Armed but zero TX frames means the interlock or the TX path is broken."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snapshot(Path(tmp), tx_frames=0)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["tx_armed"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            proc = run_cli("assess", "--status", str(path))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("tx respects the interlock", proc.stdout)

    def test_silent_tx_while_disarmed_is_correct(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snapshot(Path(tmp), tx_frames=0)
            proc = run_cli("assess", "--status", str(path))
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_json_verdict(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snapshot(Path(tmp))
            proc = run_cli("assess", "--status", str(path), "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertTrue(verdict["passed"])
        self.assertEqual(len(verdict["checks"]), 7)

    def test_missing_snapshot_exits_2(self):
        proc = run_cli("assess", "--status", "definitely-not-here.json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not found", proc.stderr)


if __name__ == "__main__":
    unittest.main()
