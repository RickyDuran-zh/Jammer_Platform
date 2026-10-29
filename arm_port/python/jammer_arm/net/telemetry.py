"""Telemetry publisher: port of ``matlab/interface/ui_send_telemetry.m``.

The UI expects exactly this packet shape (``:5555``, one JSON object per line).
Spectrum and waveform are decimated to at most ~512 points, as in MATLAB
(``ds = floor(N / 512)``), to keep the frame small enough for a 1 GbE link and a
Python consumer on the board.
"""

from __future__ import annotations

import time

import numpy as np

from ..config import JammerConfig
from ..dsp.sensing import SensingResult
from .jsonline import JsonLineConnection, LineClosed, LineTimeout

SPECTRUM_POINTS = 512


class TelemetryClient:
    def __init__(self, cfg: JammerConfig):
        n = cfg.network
        self.cfg = cfg
        self.conn = JsonLineConnection(n.host, n.telemetry_port,
                                       n.telemetry_timeout_sec, name="telemetry")
        self.frames_sent = 0
        self.frames_dropped = 0

    def connect(self) -> bool:
        return self.conn.connect()

    def close(self) -> None:
        self.conn.close()

    @property
    def connected(self) -> bool:
        return self.conn.connected

    def build_packet(self, seq: int, sensing: SensingResult, jam_signal: np.ndarray,
                     tx_state: str, action: dict, rl_meta: dict,
                     prev_action: dict | None = None,
                     hw_actuals: dict | None = None) -> dict:
        cfg = self.cfg
        spec = sensing.spectrum_abs
        f_axis = sensing.freq_axis_hz
        ds = _decimation(spec.size)
        jam_abs = np.abs(np.asarray(jam_signal).ravel())
        ds2 = _decimation(jam_abs.size)

        telemetry = {
            "snr_est": float(sensing.snr_est),
            "bw_est": float(sensing.bw_est),
            "sig_type": float(sensing.sig_type),
            "tx_state": str(tx_state),
            "spectrum": spec[::ds].astype(float).tolist(),
            "freq_axis_ghz": (f_axis[::ds] / 1e9).astype(float).tolist(),
            "jam_waveform_abs": jam_abs[::ds2].astype(float).tolist(),
            "channel_map": np.asarray(sensing.channel_map, dtype=float).tolist(),
            "channel_features": np.asarray(sensing.channel_features, dtype=float).tolist(),
        }
        if cfg.emit_hw_actuals and hw_actuals:
            # Additive extension: the legacy UI ignores unknown keys, while the
            # ARM dashboards can display what the hardware really applied.
            telemetry["hw_actuals"] = {
                "bw_mhz": float(hw_actuals.get("bw_mhz", 0.0)),
                "tx_gain_db": float(hw_actuals.get("tx_gain_db", 0.0)),
                "requested_bw_mhz": float(hw_actuals.get("requested_bw_mhz", 0.0)),
                "clipped": bool(hw_actuals.get("clipped", False)),
            }
        if prev_action is not None:
            telemetry["executed_action"] = {
                "channel_idx": int(prev_action["channel_idx"]),
                "power_db": float(prev_action["power_db"]),
                "bw_mhz": int(prev_action["bw_mhz"]),
                "waveform_mode": int(prev_action["waveform_mode"]),
            }

        return {
            "type": "telemetry",
            "version": "2.0",
            "session_id": cfg.network.session_id,
            "seq": int(seq),
            "timestamp_ms": int(time.time() * 1000),
            "telemetry": telemetry,
            "action": {
                "channel_idx": int(action["channel_idx"]),
                "power_db": float(action["power_db"]),
                "bw_mhz": int(action["bw_mhz"]),
                "waveform_mode": int(action["waveform_mode"]),
            },
            "rl_meta": dict(rl_meta),
        }

    def publish(self, packet: dict) -> bool:
        """Best-effort publish.  Returns False when the UI is not reachable."""
        if not self.conn.connected and not self.conn.connect():
            self.frames_dropped += 1
            return False
        try:
            self.conn.send(packet)
        except (LineClosed, LineTimeout, OSError):
            self.conn.close()
            self.frames_dropped += 1
            return False
        self.frames_sent += 1
        return True


def _decimation(n: int, target: int = SPECTRUM_POINTS) -> int:
    if n <= 0:
        return 1
    return max(1, int(n // target))
