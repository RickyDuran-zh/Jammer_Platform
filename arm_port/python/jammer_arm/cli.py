"""Command line interface: ``python -m jammer_arm <command>``.

Commands
--------
run         Live backend. ``--radio mock`` swaps only the device.
mock        Fully synthetic backend (no device at all) - the regression path.
doctor      Environment / hardware / safety pre-flight for the RK3588 board.
selftest    Fast self-checks of config, DSP, protocol, safety and the loop.
bench       DSP timing budget on the current machine.
assess      Evaluate a status snapshot against the acceptance thresholds (gate G4).
show-config Effective configuration as JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .config import ConfigError, JammerConfig, parse_set_args
from .core.backend import FrameReport, JammerBackend
from .core.mock_backend import MockBackend
from .dsp.sensing import sense_channel_grid
from .dsp.waveform import generate_jammer_signal, waveform_summary
from .net.command import CommandClient
from .net.jsonline import JsonLineConnection, LineServer
from .net.rl_client import RLClient
from .net.telemetry import TelemetryClient
from .radio.base import InterlockError, RadioDevice
from .version import __version__

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "jammer_arm.json"


# ══════════════════════════════════════════════════════════════════════
# parser
# ══════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jammer_arm",
        description="ARM (RK3588) jammer backend - pure Python replacement for the MATLAB backend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "RF safety: transmission stays disabled unless the environment variable\n"
            "ZH_RF_TX_ENABLE=LAB_ONLY is set before start-up. power_db=0 means minimum\n"
            "gain, not 'off'."
        ),
    )
    p.add_argument("--version", action="version", version=f"jammer_arm {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, frames: bool = True) -> None:
        sp.add_argument("--config", type=Path, default=None,
                        help=f"JSON override file (default: {DEFAULT_CONFIG.name} if present)")
        sp.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="dotted override, e.g. --set radio.num_samples=8192")
        sp.add_argument("--status", type=Path, default=None,
                        help="write a JSON status snapshot here (atomic replace)")
        sp.add_argument("--quiet", action="store_true", help="only print warnings and the summary")
        if frames:
            sp.add_argument("--duration", type=float, default=None,
                            help="stop after N seconds (default: run until Ctrl+C)")
            sp.add_argument("--frames", type=int, default=None, help="stop after N frames")
            sp.add_argument("--warmup", type=int, default=0,
                            help="exclude the first N frames from the stability "
                                 "counters (connection/PPO/cache warm-up)")

    def peers(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--no-rl", action="store_true", help="do not contact the RL service")
        sp.add_argument("--no-telemetry", action="store_true", help="do not publish telemetry")
        sp.add_argument("--no-cmd", action="store_true", help="do not poll the command bridge")

    run = sub.add_parser("run", help="live backend (UHD, rfio daemon, or mock radio)")
    run.add_argument("--radio", choices=["uhd", "rfio", "mock"], default="uhd")
    run.add_argument("--rfio-port", type=int, default=5601)
    run.add_argument("--control", choices=["rl", "agent"], default=None,
                     help="initial control owner (default: rl)")
    common(run)
    peers(run)

    mock = sub.add_parser("mock", help="fully synthetic backend (no device)")
    mock.add_argument("--seed", type=int, default=None)
    mock.add_argument("--control", choices=["rl", "agent"], default=None)
    common(mock)
    peers(mock)

    doctor = sub.add_parser("doctor", help="pre-flight checks for this machine")
    doctor.add_argument("--config", type=Path, default=None)
    doctor.add_argument("--set", action="append", default=[])
    doctor.add_argument("--quiet", action="store_true")

    self_test = sub.add_parser("selftest", help="run internal self-checks")
    self_test.add_argument("--config", type=Path, default=None)
    self_test.add_argument("--set", action="append", default=[])
    self_test.add_argument("--quiet", action="store_true")

    bench = sub.add_parser("bench", help="DSP timing budget")
    bench.add_argument("--iterations", type=int, default=200)
    bench.add_argument("--config", type=Path, default=None)
    bench.add_argument("--set", action="append", default=[])

    assess = sub.add_parser(
        "assess", help="evaluate a status snapshot against the P6 acceptance thresholds")
    assess.add_argument("--status", type=Path, required=True)
    assess.add_argument("--min-frames", type=int, default=1)
    assess.add_argument("--max-frame-factor", type=float, default=2.0,
                        help="fail if max frame time exceeds N x the frame period (default 2.0)")
    assess.add_argument("--json", action="store_true", help="machine-readable verdict")
    assess.add_argument("--quiet", action="store_true")

    show = sub.add_parser("show-config", help="print the effective configuration")
    show.add_argument("--config", type=Path, default=None)
    show.add_argument("--set", action="append", default=[])

    return p


# ══════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════


def load_config(args) -> JammerConfig:
    path = getattr(args, "config", None)
    if path is None and DEFAULT_CONFIG.is_file():
        path = DEFAULT_CONFIG
    return JammerConfig.load(path, parse_set_args(getattr(args, "set", []) or []))


def make_radio(cfg: JammerConfig, kind: str, port: int = 5601) -> RadioDevice:
    if kind == "uhd":
        from .radio.uhd_device import UhdRadio
        return UhdRadio(cfg)
    if kind == "rfio":
        from .radio.rfio import RfioRadio
        return RfioRadio(cfg, port=port)
    from .radio.mock import MockRadio
    return MockRadio(cfg)


def print_frame(report: FrameReport, quiet: bool = False) -> None:
    if quiet:
        return
    a = report.action
    print(
        f"[SEQ={report.seq}] ch={a['channel_idx'] + 1} power={a['power_db']:.1f}dB "
        f"bw={a['bw_mhz']}MHz mode={a['waveform_mode']} policy={report.rl_meta.get('policy')} "
        f"ctrl={report.control} rx={'ok' if report.rx_ok else 'cached'} "
        f"tx={report.tx_state} frame={report.frame_ms:.1f}ms dsp={report.dsp_ms:.2f}ms",
        flush=True,
    )


def install_signal_handlers(backend: JammerBackend) -> None:
    def _handler(signum, _frame):
        print(f"\n[jammer_arm] signal {signum}: stopping after the current frame", flush=True)
        backend.stop()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def _assemble(cfg: JammerConfig, backend: JammerBackend, args) -> JammerBackend:
    backend.rl = None if getattr(args, "no_rl", False) else RLClient(cfg)
    backend.telemetry = None if getattr(args, "no_telemetry", False) else TelemetryClient(cfg)
    backend.commands = None if getattr(args, "no_cmd", False) else CommandClient(cfg)
    if args.control == "agent":
        backend.state.control.agent_control_mode = True
    return backend


# ══════════════════════════════════════════════════════════════════════
# run / mock
# ══════════════════════════════════════════════════════════════════════


def cmd_run(args) -> int:
    cfg = load_config(args)
    radio = make_radio(cfg, args.radio, args.rfio_port)
    backend = JammerBackend(cfg, radio, logger=lambda m: print(m, flush=True),
                            status_path=args.status, warmup_frames=args.warmup)
    backend = _assemble(cfg, backend, args)
    return _execute(cfg, backend, args)


def cmd_mock(args) -> int:
    cfg = load_config(args)
    backend = MockBackend(cfg, seed=args.seed, logger=lambda m: print(m, flush=True),
                          status_path=args.status, warmup_frames=args.warmup)
    backend = _assemble(cfg, backend, args)
    return _execute(cfg, backend, args)


def _execute(cfg: JammerConfig, backend: JammerBackend, args) -> int:
    armed = cfg.tx_armed()
    print(f"[jammer_arm] mode={backend.mode} radio={backend.radio.info.backend} "
          f"tx_armed={'YES' if armed else 'no'} "
          f"(set {cfg.safety.interlock_env}={cfg.safety.interlock_value} to arm)", flush=True)
    if not armed:
        print("[jammer_arm] RF transmission stays disabled; the loop still senses, "
              "decides and reports tx_state=disabled.", flush=True)

    install_signal_handlers(backend)
    try:
        backend.radio.open()
    except Exception as exc:  # noqa: BLE001 - reported to the operator
        print(f"[jammer_arm] FATAL: cannot open radio: {exc}", file=sys.stderr, flush=True)
        backend.close()
        return 2

    if backend.rl is not None and not backend.rl.connect():
        print(f"[jammer_arm] RL service not reachable at "
              f"{cfg.network.host}:{cfg.network.rl_port}; frames will use the fallback action.",
              flush=True)
    if backend.telemetry is not None and not backend.telemetry.connect():
        print(f"[jammer_arm] UI telemetry not reachable at "
              f"{cfg.network.host}:{cfg.network.telemetry_port}; frames are not displayed.",
              flush=True)
    if backend.commands is not None and not backend.commands.connect():
        print(f"[jammer_arm] command bridge not reachable at "
              f"{cfg.network.host}:{cfg.network.command_port}; running open-loop.", flush=True)

    started = time.time()
    try:
        status = backend.run(duration_sec=args.duration, max_frames=args.frames,
                             on_frame=lambda r: print_frame(r, args.quiet))
    finally:
        backend.close()

    elapsed = max(time.time() - started, 1e-9)
    m = status["metrics"]
    print("\n[jammer_arm] summary", flush=True)
    print(f"  frames          : {m['frames']} in {elapsed:.1f}s "
          f"({m['frames'] / elapsed:.2f} fps)", flush=True)
    print(f"  frame time      : last {m['last_frame_ms']:.1f} ms, "
          f"max {m['max_frame_ms']:.1f} ms", flush=True)
    print(f"  dsp time        : last {m['last_dsp_ms']:.2f} ms, "
          f"max {m['max_dsp_ms']:.2f} ms", flush=True)
    print(f"  rx failures     : {m['rx_failures']} (rebuilds {m['rx_rebuilds']}, "
          f"full {m['radio_rebuilds']})", flush=True)
    print(f"  tx              : {m['tx_frames']} frames, {m['tx_errors']} errors, "
          f"{m['interlock_blocks']} interlock blocks", flush=True)
    print(f"  overruns        : {m['overruns']}", flush=True)
    if args.status:
        print(f"  status snapshot : {args.status}", flush=True)
    return 0


# ══════════════════════════════════════════════════════════════════════
# doctor
# ══════════════════════════════════════════════════════════════════════


def _free_port(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR here: on Windows it would let us bind a port that is
        # already listening, which defeats the purpose of the check.
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def cmd_doctor(args) -> int:
    cfg = load_config(args)
    rows: list[tuple[str, str, str]] = []

    def add(check: str, ok: bool, detail: str, warn_only: bool = False) -> None:
        rows.append((check, "PASS" if ok else ("WARN" if warn_only else "FAIL"), detail))

    add("python", sys.version_info >= (3, 9),
        f"{platform.python_version()} ({platform.machine()})")
    add("numpy", True, np.__version__)
    try:
        from numpy import fft  # noqa: F401
        add("numpy.fft", True, "pocketfft backend available")
    except Exception as exc:  # noqa: BLE001
        add("numpy.fft", False, str(exc))

    # UHD
    try:
        import uhd  # noqa: PLC0415
        version = ""
        for attr in ("get_version_string",):
            fn = getattr(uhd, attr, None)
            if callable(fn):
                try:
                    version = str(fn())
                except Exception:
                    pass
        add("uhd python bindings", True, version or getattr(uhd, "__file__", "imported"))
    except Exception as exc:  # noqa: BLE001
        add("uhd python bindings", False,
            f"not importable ({exc}) - build UHD with -DENABLE_PYTHON_API=ON "
            "or use --radio rfio", warn_only=True)

    # ports
    for label, port in (("rl", cfg.network.rl_port),
                        ("telemetry", cfg.network.telemetry_port),
                        ("command", cfg.network.command_port)):
        listening = not _free_port(cfg.network.host, port)
        # telemetry/command are expected to be served by the UI; rl by the service
        add(f"port {port} ({label})", True,
            "already in use (peer probably running)" if listening else "free")

    # safety
    armed = cfg.tx_armed()
    add("rf interlock", True,
        f"ARMED via {cfg.safety.interlock_env}"
        if armed else f"disarmed (set {cfg.safety.interlock_env}={cfg.safety.interlock_value} "
                      "only in an authorized shielded/cabled setup)")

    # platform tuning hints
    gov_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    if gov_path.is_file():
        gov = gov_path.read_text().strip()
        add("cpu governor", gov == "performance",
            f"{gov} (use `cpupower frequency-set -g performance` for lower jitter)",
            warn_only=True)
    for label, path, want in (
        ("net core rmem", "/proc/sys/net/core/rmem_max", 8 * 1024 * 1024),
        ("net core wmem", "/proc/sys/net/core/wmem_max", 8 * 1024 * 1024),
    ):
        p = Path(path)
        if p.is_file():
            try:
                value = int(p.read_text().strip())
                add(label, value >= want, f"{value} (recommended >= {want})", warn_only=True)
            except ValueError:
                add(label, False, "unreadable", warn_only=True)

    if platform.system() == "Linux":
        try:
            policy = os.sched_getscheduler(0)
            add("scheduler", True, {0: "SCHED_OTHER", 1: "SCHED_FIFO", 2: "SCHED_RR"}.get(
                policy, str(policy)))
        except (AttributeError, OSError):
            pass

    width = max(len(r[0]) for r in rows) if rows else 10
    for check, verdict, detail in rows:
        if args.quiet and verdict == "PASS":
            continue
        print(f"[{verdict:4}] {check:<{width}}  {detail}")
    failures = sum(1 for r in rows if r[1] == "FAIL")
    print(f"\n{len(rows)} checks, {failures} failure(s)")
    return 1 if failures else 0


# ══════════════════════════════════════════════════════════════════════
# selftest
# ══════════════════════════════════════════════════════════════════════


class _Checker:
    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, fn) -> None:
        t0 = time.perf_counter()
        try:
            detail = fn() or ""
            ok = True
        except Exception as exc:  # noqa: BLE001 - reported as a failure
            detail = f"{type(exc).__name__}: {exc}"
            ok = False
        dt = (time.perf_counter() - t0) * 1000
        self.results.append((name, ok, f"{detail} [{dt:.1f} ms]"))
        if not self.quiet or not ok:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    @property
    def failures(self) -> int:
        return sum(1 for _, ok, _ in self.results if not ok)


def cmd_selftest(args) -> int:
    cfg = load_config(args)
    c = _Checker(args.quiet)

    def _config():
        assert cfg.radio.num_samples == 4096
        assert abs(cfg.radio.sample_rate_hz - 20e6) < 1
        assert cfg.spectrum.num_channels == 10
        assert len(cfg.channel_centers_hz) == 10
        return f"10 channels @ {cfg.radio.sample_rate_hz / 1e6:.0f} MS/s, fc={cfg.radio.center_freq_hz / 1e9:.2f} GHz"

    def _sensing():
        rng = np.random.default_rng(7)
        n = cfg.radio.num_samples
        t = np.arange(n) / cfg.radio.sample_rate_hz
        rx = 0.02 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
        rx += 0.6 * np.exp(1j * 2 * np.pi * (cfg.channel_centers_hz[4] - cfg.radio.center_freq_hz) * t)
        res = sense_channel_grid(rx, cfg, {"channel_idx": 4, "bw_mhz": 4})
        assert res.channel_features.shape == (10, 4)
        assert np.all(np.isfinite(res.channel_features))
        assert res.channel_map.max() >= 1
        hot = int(np.argmax(res.channel_features[:, 0]))
        assert hot == 4, f"occupancy peak at channel {hot}, expected 4"
        return f"peak channel {hot + 1}, snr_est={res.snr_est:.2f} dB, bw_est={res.bw_est / 1e6:.1f} MHz"

    def _waveform_noise():
        y, gain = generate_jammer_signal(
            {"channel_idx": 3, "power_db": 10, "bw_mhz": 4, "waveform_mode": 0}, cfg)
        peak = float(np.max(np.abs(y)))
        assert abs(peak - cfg.radio.tx_baseband_peak) < 1e-9, peak
        assert np.all(np.isfinite(y))
        assert gain == 5.0, gain
        s = waveform_summary(y, cfg.radio.sample_rate_hz)
        return f"peak={s['peak']:.3f}, rms={s['rms']:.4f}, bw={s['occupied_bw_hz'] / 1e6:.1f} MHz, gain={gain}"

    def _waveform_tone():
        a = {"channel_idx": 0, "power_db": 20, "bw_mhz": 8, "waveform_mode": 1}
        y1, g1 = generate_jammer_signal(a, cfg)
        y2, _ = generate_jammer_signal(a, cfg)
        assert np.allclose(y1, y2), "multi-tone mode must be deterministic"
        assert g1 == cfg.radio.tx_gain_max_db
        s = waveform_summary(y1, cfg.radio.sample_rate_hz)
        return f"deterministic, bw={s['occupied_bw_hz'] / 1e6:.1f} MHz, gain={g1}"

    def _interlock():
        from .radio.mock import MockRadio
        radio = MockRadio(cfg)
        frame = np.zeros(cfg.radio.num_samples, dtype=np.complex64)
        env = {cfg.safety.interlock_env: ""}
        try:
            radio.transmit(frame, 0.0, env=env)
        except InterlockError:
            pass
        else:
            raise AssertionError("transmit was allowed without the interlock")
        radio.transmit(frame, 0.0, env={cfg.safety.interlock_env: cfg.safety.interlock_value})
        assert len(radio.tx_frames) == 1
        return "fake interlock blocked, armed interlock allowed"

    def _jsonline():
        port = _pick_port()
        server = LineServer("127.0.0.1", port, lambda pkt: {"type": "pong", "echo": pkt.get("type")}).start()
        try:
            conn = JsonLineConnection("127.0.0.1", port, timeout=1.0, name="test")
            assert conn.connect()
            rsp = conn.request({"type": "ping"})
            assert rsp.get("type") == "pong" and rsp.get("echo") == "ping"
            conn.close()
        finally:
            server.stop()
        return "request/response round-trip on loopback"

    def _loop():
        backend = MockBackend(cfg, seed=11)
        reports = []

        def on_frame(r):
            reports.append(r)

        backend.run(max_frames=5, on_frame=on_frame)
        assert len(reports) == 5
        assert all(r.tx_state == "mock_running" for r in reports)
        actions = {json.dumps(r.action, sort_keys=True) for r in reports}
        return f"5 synthetic frames, {len(actions)} distinct actions, tx_state=mock_running"

    def _recovery():
        from .radio.mock import MockRadio
        radio = MockRadio(cfg, seed=3)
        radio.open()
        backend = JammerBackend(cfg, radio)
        backend.step()                      # warms the sensing cache
        radio.fail_always(True)
        for _ in range(cfg.loop.rx_timeout_reinit_threshold + 2):
            report = backend.step()
            assert report.rx_ok is False
        assert backend.metrics["rx_failures"] >= 1
        assert backend.metrics["rx_rebuilds"] >= 1 or backend.metrics["radio_rebuilds"] >= 1
        radio.fail_always(False)
        assert backend.step().rx_ok is True
        return (f"{backend.metrics['rx_failures']} injected failures handled, "
                f"rebuilds={backend.metrics['rx_rebuilds']}, cache kept the loop alive")

    c.check("config", _config)
    c.check("sensing", _sensing)
    c.check("waveform/noise", _waveform_noise)
    c.check("waveform/tone", _waveform_tone)
    c.check("rf interlock", _interlock)
    c.check("json-lines transport", _jsonline)
    c.check("mock frame loop", _loop)
    c.check("rx recovery ladder", _recovery)

    print(f"\n{len(c.results)} checks, {c.failures} failure(s)")
    return 1 if c.failures else 0


def _pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ══════════════════════════════════════════════════════════════════════
# bench
# ══════════════════════════════════════════════════════════════════════


def cmd_bench(args) -> int:
    cfg = load_config(args)
    n = cfg.radio.num_samples
    rng = np.random.default_rng(0)
    rx = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
    prev = {"channel_idx": 4, "power_db": 10, "bw_mhz": 4, "waveform_mode": 0}

    def timeit(fn, iterations):
        samples = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - t0) * 1000.0)
        a = np.asarray(samples)
        return a.mean(), float(np.percentile(a, 95)), a.max()

    cases = {
        "sensing (fft+10ch)": lambda: sense_channel_grid(rx, cfg, prev),
        "waveform noise (fir)": lambda: generate_jammer_signal(prev, cfg),
        "waveform noise (fft)": lambda: generate_jammer_signal(prev, cfg, shaping="fft"),
        "waveform tone": lambda: generate_jammer_signal(
            {**prev, "waveform_mode": 1}, cfg),
    }
    telemetry = TelemetryClient(cfg)
    sensing = sense_channel_grid(rx, cfg, prev)
    frame, _ = generate_jammer_signal(prev, cfg)
    packet = telemetry.build_packet(1, sensing, frame, "running", prev,
                                    {"policy": "ppo", "latency_ms": 1})
    payload = json.dumps(packet)
    cases["telemetry packet build"] = lambda: telemetry.build_packet(
        1, sensing, frame, "running", prev, {"policy": "ppo", "latency_ms": 1})
    cases["telemetry json encode"] = lambda: json.dumps(packet)

    period_ms = cfg.loop.frame_period_sec * 1000.0
    print(f"machine      : {platform.machine()} / {platform.processor() or platform.system()}")
    print(f"numpy        : {np.__version__}")
    print(f"frame budget : {period_ms:.0f} ms ({1 / cfg.loop.frame_period_sec:.1f} fps), "
          f"{n} samples per frame")
    print(f"telemetry    : {len(payload) / 1024:.1f} KiB per frame\n")
    print(f"{'case':<28}{'mean':>9}{'p95':>9}{'max':>9}{'% budget':>11}")
    worst = 0.0
    for name, fn in cases.items():
        mean, p95, mx = timeit(fn, args.iterations)
        worst = max(worst, mean)
        print(f"{name:<28}{mean:>8.3f}ms{p95:>8.3f}ms{mx:>8.3f}ms"
              f"{100 * mean / period_ms:>10.2f}%")
    print(f"\nworst single-stage mean: {worst:.3f} ms = {100 * worst / period_ms:.2f}% of the frame budget")
    print("Design target: full frame (sense+waveform+telemetry) stays below 25% of the budget,")
    print("leaving the rest for UHD transport, the RL round-trip and OS jitter.")
    return 0


# ══════════════════════════════════════════════════════════════════════
# assess  (gate G4 of the rollout plan, machine-checkable)
# ══════════════════════════════════════════════════════════════════════


def assess_snapshot(data: dict, min_frames: int = 1,
                    max_frame_factor: float = 2.0) -> list[tuple[str, bool, str]]:
    """Turn a ``--status`` snapshot into pass/fail criteria.

    These are the thresholds from ``docs/02_plan.md`` gate G4, expressed as code so
    the board run cannot be declared healthy by eyeballing the log.
    """
    m = data.get("metrics") or {}
    frames = int(m.get("frames", 0))
    period_ms = float(data.get("frame_period_ms") or 80.0)
    max_frame = float(m.get("max_frame_ms", 0.0))
    armed = bool(data.get("tx_armed"))
    tx_frames = int(m.get("tx_frames", 0))
    tx_errors = int(m.get("tx_errors", 0))

    checks = [
        ("frames collected", frames >= min_frames,
         f"{frames} >= {min_frames}"),
        ("no rx failures", int(m.get("rx_failures", 0)) == 0,
         f"rx_failures={m.get('rx_failures', 0)}"),
        ("no rx overruns", int(m.get("overruns", 0)) == 0,
         f"overruns={m.get('overruns', 0)}"),
        ("frame time within budget", max_frame <= max_frame_factor * period_ms,
         f"max={max_frame:.1f} ms vs limit {max_frame_factor * period_ms:.1f} ms"),
        ("no unexpected frame errors", int(m.get("frame_errors", 0)) == 0,
         f"frame_errors={m.get('frame_errors', 0)}"),
        ("tx respects the interlock", (tx_frames > 0) if armed else (tx_frames == 0),
         f"tx_armed={armed}, tx_frames={tx_frames}"),
        ("no tx errors", tx_errors == 0, f"tx_errors={tx_errors}"),
    ]
    return checks


def cmd_assess(args) -> int:
    if not args.status.is_file():
        print(f"[assess] status snapshot not found: {args.status}", file=sys.stderr)
        return 2
    try:
        data = json.loads(args.status.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[assess] cannot read {args.status}: {exc}", file=sys.stderr)
        return 2

    checks = assess_snapshot(data, args.min_frames, args.max_frame_factor)
    failed = [c for c in checks if not c[1]]

    if args.json:
        print(json.dumps({
            "status": args.status.as_posix(),
            "mode": data.get("mode"),
            "tx_armed": data.get("tx_armed"),
            "metrics": data.get("metrics"),
            "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks],
            "passed": not failed,
        }, ensure_ascii=False, indent=2))
    else:
        if not args.quiet:
            print(f"status   : {args.status}  (mode={data.get('mode')}, "
                  f"tx_armed={data.get('tx_armed')})")
        width = max(len(c[0]) for c in checks)
        for name, ok, detail in checks:
            if args.quiet and ok:
                continue
            print(f"[{'PASS' if ok else 'FAIL'}] {name:<{width}}  {detail}")
        print()
        if failed:
            print(f"assessment: {len(failed)} criterion(s) FAILED")
        else:
            print(f"assessment: ALL {len(checks)} CRITERIA PASSED")
    return 1 if failed else 0


# ══════════════════════════════════════════════════════════════════════
# show-config
# ══════════════════════════════════════════════════════════════════════


def cmd_show_config(args) -> int:
    cfg = load_config(args)
    print(json.dumps(cfg.as_dict(), ensure_ascii=False, indent=2))
    return 0


# ══════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "mock":
            return cmd_mock(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "selftest":
            return cmd_selftest(args)
        if args.command == "bench":
            return cmd_bench(args)
        if args.command == "assess":
            return cmd_assess(args)
        if args.command == "show-config":
            return cmd_show_config(args)
    except ConfigError as exc:
        print(f"[jammer_arm] configuration error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
