"""The frame loop: port of ``matlab/backend/jammer_backend_loop.m``.

Per frame, in the same order as the MATLAB backend::

    poll UI commands -> RX a frame -> FFT sensing -> decide (agent | RL)
    -> generate waveform -> TX (only with an armed interlock) -> publish telemetry

Everything that made the MATLAB loop survivable on real hardware is preserved:

* RX timeout counter with a two-stage recovery ladder (rebuild RX, then rebuild the
  whole chain) before falling back to the previous sensing snapshot;
* cached sensing so telemetry and decisions keep flowing while the radio is down;
* one bad frame never terminates the loop;
* the newest action wins - a partially delivered command set can never replay a
  stale action after a control-ownership switch.

On top of that the port adds measurement: per-frame timing, DSP cost, RX failure
counters and a JSON status snapshot for the deployment dashboards.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import JammerConfig
from ..dsp.sensing import SensingResult, sense_channel_grid
from ..dsp.waveform import effective_bandwidth_hz, generate_jammer_signal
from ..net.command import CommandClient
from ..net.rl_client import RLClient
from ..net.telemetry import TelemetryClient
from ..radio.base import InterlockError, RadioDevice, RadioError
from .commands import ControlState, apply_command

Logger = Callable[[str], None]


@dataclass
class BackendState:
    seq: int = 0
    prev_action: dict = field(default_factory=lambda: {
        "channel_idx": 0, "power_db": 0.0, "bw_mhz": 4, "waveform_mode": 0,
    })
    tx_state: str = "disabled"
    control: ControlState = field(default_factory=ControlState)
    last_sensing: SensingResult | None = None
    rx_timeout_count: int = 0
    rx_reinit_fail_count: int = 0
    frames: int = 0


@dataclass
class FrameReport:
    seq: int
    action: dict
    rl_meta: dict
    sensing: SensingResult
    tx_state: str
    rx_ok: bool
    frame_ms: float
    dsp_ms: float
    control: str
    overrun: bool = False


class JammerBackend:
    """Live backend: real radio + real DSP + real decision path."""

    mode = "live"

    def __init__(self, cfg: JammerConfig, radio: RadioDevice,
                 rl: RLClient | None = None,
                 telemetry: TelemetryClient | None = None,
                 commands: CommandClient | None = None,
                 logger: Logger | None = None,
                 status_path: str | os.PathLike | None = None,
                 warmup_frames: int = 0):
        self.cfg = cfg
        self.radio = radio
        self.rl = rl
        self.telemetry = telemetry
        self.commands = commands
        self._log = logger or (lambda msg: print(msg, flush=True))
        self.state = BackendState()
        self._stop = threading.Event()
        self.status_path = Path(status_path) if status_path else None
        #: Frames excluded from the stability statistics.  The first frames carry
        #: one-off costs (TCP connect, torch warm-up, page faults, USRP settling)
        #: that say nothing about steady-state behaviour.
        self.warmup_frames = max(0, int(warmup_frames))
        self._last_status_write = 0.0
        self.metrics = {
            "frames": 0,
            "rx_failures": 0,
            "rx_rebuilds": 0,
            "radio_rebuilds": 0,
            "tx_frames": 0,
            "tx_errors": 0,
            "interlock_blocks": 0,
            "overruns": 0,
            "last_frame_ms": 0.0,
            "last_dsp_ms": 0.0,
            "max_frame_ms": 0.0,
            "max_dsp_ms": 0.0,
            "telemetry_frames": 0,
            "rl_fallbacks": 0,
            "frame_errors": 0,
            "warmup_frames": self.warmup_frames,
        }

    # ── template hooks ───────────────────────────────────────────────
    @property
    def frame_period_sec(self) -> float:
        return self.cfg.loop.frame_period_sec

    def _acquire_sensing(self) -> tuple[SensingResult, bool]:
        """RX one frame and sense it; fall back to the cached snapshot."""
        rx = self._receive_with_recovery()
        if rx is None:
            cached = self.state.last_sensing
            if cached is None:
                cached = SensingResult.empty(self.cfg)
            return cached, False
        sensing = sense_channel_grid(rx, self.cfg, self.state.prev_action)
        self.state.last_sensing = sensing
        return sensing, True

    def _transmit(self, frame: np.ndarray, gain_db: float) -> str:
        if not self.cfg.tx_armed():
            self.metrics["interlock_blocks"] += 1
            return "disabled"
        try:
            state = self.radio.transmit(frame, gain_db)
            self.metrics["tx_frames"] += 1
            return state
        except InterlockError:
            self.metrics["interlock_blocks"] += 1
            return "disabled"
        except RadioError as exc:
            self.metrics["tx_errors"] += 1
            self._log(f"[TX] warning: {exc}")
            return "error"

    # ── recovery ladder ──────────────────────────────────────────────
    def _receive_with_recovery(self) -> np.ndarray | None:
        cfg = self.cfg
        st = self.state
        try:
            samples = self.radio.receive(int(cfg.radio.num_samples))
            st.rx_timeout_count = 0
            st.rx_reinit_fail_count = 0
            return samples
        except RadioError as exc:
            st.rx_timeout_count += 1
            self.metrics["rx_failures"] += 1
            self._log(
                f"[RX] receive failed ({st.rx_timeout_count}/"
                f"{cfg.loop.rx_timeout_reinit_threshold}): {exc}"
            )

        if st.rx_timeout_count < cfg.loop.rx_timeout_reinit_threshold:
            return None

        st.rx_timeout_count = 0
        try:
            time.sleep(cfg.loop.rx_reinit_pause_sec)
            self.radio.rebuild_rx()
            st.rx_reinit_fail_count = 0
            self.metrics["rx_rebuilds"] += 1
            self._log("[RX] receiver rebuilt after timeout burst")
            return None
        except Exception as exc:  # noqa: BLE001 - any failure escalates
            st.rx_reinit_fail_count += 1
            self._log(
                f"[RX] receiver rebuild failed ({st.rx_reinit_fail_count}/"
                f"{cfg.loop.full_radio_reinit_threshold}): {exc}"
            )

        if st.rx_reinit_fail_count >= cfg.loop.full_radio_reinit_threshold:
            try:
                time.sleep(cfg.loop.rx_reinit_pause_sec)
                self.radio.rebuild()
                st.rx_reinit_fail_count = 0
                self.metrics["radio_rebuilds"] += 1
                self._log("[RX] full radio chain rebuilt")
            except Exception as exc:  # noqa: BLE001
                self._log(f"[RX] full radio chain rebuild failed: {exc}")
        return None

    # ── one frame ────────────────────────────────────────────────────
    def step(self) -> FrameReport:
        cfg = self.cfg
        st = self.state
        t_start = time.perf_counter()
        st.seq += 1

        # 1. control commands from the UI / Agent
        self._poll_commands()

        # 2. RX + sensing (cached when the radio is unavailable)
        sensing, rx_ok = self._acquire_sensing()

        # 3. decision
        if st.control.agent_control_mode:
            action = dict(st.prev_action)
            rl_meta = {"policy": "agent", "value": 0, "latency_ms": 0}
        elif self.rl is not None:
            action, rl_meta = self.rl.request_action(
                st.seq, sensing.channel_features, st.prev_action, st.tx_state)
            if rl_meta.get("policy") == "fallback":
                self.metrics["rl_fallbacks"] += 1
        else:
            action, rl_meta = dict(st.prev_action), {"policy": "none", "value": 0, "latency_ms": 0}

        action = cfg.actions.coerce(action["channel_idx"], action["power_db"],
                                    action["bw_mhz"], action["waveform_mode"],
                                    cfg.spectrum.num_channels)

        # 4. waveform
        t_dsp = time.perf_counter()
        frame, gain = generate_jammer_signal(action, cfg)
        dsp_ms = (time.perf_counter() - t_dsp) * 1000.0

        # 5. TX (interlock enforced inside the device as well)
        tx_state = self._transmit(frame, gain)

        # 6. telemetry + bookkeeping
        st.prev_action = dict(action)
        st.tx_state = tx_state
        st.frames += 1
        self.metrics["frames"] += 1

        frame_ms = (time.perf_counter() - t_start) * 1000.0
        self.metrics["last_frame_ms"] = frame_ms
        self.metrics["last_dsp_ms"] = dsp_ms
        self.metrics["max_frame_ms"] = max(self.metrics["max_frame_ms"], frame_ms)
        self.metrics["max_dsp_ms"] = max(self.metrics["max_dsp_ms"], dsp_ms)
        overrun = frame_ms > self.frame_period_sec * 1000.0
        if overrun:
            self.metrics["overruns"] += 1

        self._publish(st.seq, sensing, frame, tx_state, action, rl_meta)

        return FrameReport(
            seq=st.seq, action=action, rl_meta=rl_meta, sensing=sensing,
            tx_state=tx_state, rx_ok=rx_ok, frame_ms=frame_ms, dsp_ms=dsp_ms,
            control=st.control.control_label, overrun=overrun,
        )

    def _poll_commands(self) -> None:
        if self.commands is None:
            return
        for cmd in self.commands.poll():
            self.state.prev_action = apply_command(
                cmd, self.state.prev_action, self.state.control, self.cfg)
        while self.state.control.messages:
            self._log(self.state.control.messages.pop(0))

    def _publish(self, seq: int, sensing: SensingResult, frame: np.ndarray,
                 tx_state: str, action: dict, rl_meta: dict) -> None:
        if self.telemetry is None:
            return
        f0 = float(self.cfg.channel_centers_hz[action["channel_idx"]]
                   - self.cfg.radio.center_freq_hz)
        bw_eff = effective_bandwidth_hz(action["bw_mhz"], f0, self.cfg.radio.sample_rate_hz)
        packet = self.telemetry.build_packet(
            seq, sensing, frame, tx_state, action, rl_meta,
            prev_action=self.state.prev_action,
            hw_actuals={
                "bw_mhz": bw_eff / 1e6,
                "requested_bw_mhz": float(action["bw_mhz"]),
                "tx_gain_db": float(self.radio.info.tx_gain_db),
                "clipped": bw_eff < float(action["bw_mhz"]) * 1e6 - 1.0,
            },
        )
        if self.telemetry.publish(packet):
            self.metrics["telemetry_frames"] += 1

    # ── run loop ─────────────────────────────────────────────────────
    def run(self, duration_sec: float | None = None,
            max_frames: int | None = None,
            stop_event: threading.Event | None = None,
            on_frame: Callable[[FrameReport], None] | None = None) -> dict:
        """Run frames at a fixed cadence until stopped.

        Pacing is deadline based: the period does not drift when a frame is slow,
        and a persistent overrun is counted instead of silently shortening the
        loop.
        """
        period = float(self.frame_period_sec)
        t_next = time.monotonic()
        t_end = None if duration_sec is None else t_next + float(duration_sec)
        stop_event = stop_event or self._stop
        attempts = 0

        while not stop_event.is_set():
            attempts += 1
            # One bad frame must never end the run (MATLAB: catch / continue).
            # Anything that escapes the recovery ladder is logged and counted.
            try:
                report = self.step()
            except Exception as exc:  # noqa: BLE001 - loop-level isolation
                self.metrics["frame_errors"] += 1
                self._log(
                    f"[LOOP] frame {attempts} failed, continuing: "
                    f"{type(exc).__name__}: {exc}"
                )
                report = None
            if report is not None and on_frame is not None:
                on_frame(report)
            if self.warmup_frames and attempts == self.warmup_frames:
                self._reset_quality_metrics()
                self._log(f"[LOOP] warm-up complete ({attempts} frames), "
                          "stability counters reset")
            self._maybe_write_status()

            t_next += period
            now = time.monotonic()
            if t_next < now:
                # A slow frame: resynchronise instead of accumulating debt.
                t_next = now
            else:
                stop_event.wait(t_next - now)

            if max_frames is not None and attempts >= max_frames:
                break
            if t_end is not None and time.monotonic() >= t_end:
                break

        # Always leave a snapshot that reflects the whole run, so the acceptance
        # gate (`jammer_arm assess`) sees the final counters instead of the first
        # periodic write.
        self._maybe_write_status(force=True)
        return self.status()

    def _reset_quality_metrics(self) -> None:
        """Drop warm-up counters so the gate judges steady state only."""
        for key in ("rx_failures", "rx_rebuilds", "radio_rebuilds", "tx_errors",
                    "interlock_blocks", "overruns", "frame_errors", "rl_fallbacks"):
            self.metrics[key] = 0
        self.metrics["max_frame_ms"] = self.metrics["last_frame_ms"]
        self.metrics["max_dsp_ms"] = self.metrics["last_dsp_ms"]

    def stop(self) -> None:
        self._stop.set()

    # ── status / shutdown ────────────────────────────────────────────
    def status(self) -> dict:
        st = self.state
        return {
            "mode": self.mode,
            "session_id": self.cfg.network.session_id,
            "seq": st.seq,
            "control": st.control.control_label,
            "action": dict(st.prev_action),
            "tx_state": st.tx_state,
            "tx_armed": self.cfg.tx_armed(),
            "frame_period_ms": self.frame_period_sec * 1000.0,
            "interlock_env": self.cfg.safety.interlock_env,
            "radio": self.radio.info.as_dict(),
            "metrics": dict(self.metrics),
            "peers": {
                "rl": bool(self.rl and self.rl.conn.connected),
                "telemetry": bool(self.telemetry and self.telemetry.connected),
                "commands": bool(self.commands and self.commands.connected),
            },
            "timestamp_ms": int(time.time() * 1000),
        }

    def _maybe_write_status(self, force: bool = False) -> None:
        if self.status_path is None:
            return
        now = time.monotonic()
        if not force and now - self._last_status_write < self.cfg.loop.status_write_interval_sec:
            return
        self._last_status_write = now
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_suffix(self.status_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self.status(), fh, ensure_ascii=False, indent=2)
            tmp.replace(self.status_path)
        except OSError:
            pass

    def close(self) -> None:
        self.stop()
        for peer in (self.commands, self.rl, self.telemetry):
            if peer is not None:
                try:
                    peer.close()
                except Exception:
                    pass
        try:
            self.radio.close()
        except Exception:
            pass
