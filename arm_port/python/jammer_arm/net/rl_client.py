"""RL decision client: port of ``matlab/interface/rl_bridge_step.m``.

Wire contract (unchanged, so the existing ``python/rl_service`` and the PyQt UI
keep working):

    -> {"type":"state_req", "version":"2.0", "session_id":..., "seq":...,
        "state":{"task":"jammer_spectrum_ctrl","num_channels":10,
                 "channel_features":[[...]x10],"prev_action":{...},"tx_state":"..."}}
    <- {"type":"action_rsp","status":"ok","action":{...},"rl_meta":{"policy":"ppo",...}}

If the service is unavailable the caller keeps the previous action and marks the
frame ``policy="fallback"`` -- identical to the MATLAB behaviour.
"""

from __future__ import annotations

import time

import numpy as np

from ..config import JammerConfig
from .jsonline import JsonLineConnection, LineClosed, LineTimeout

TASK_NAME = "jammer_spectrum_ctrl"


class RLClient:
    def __init__(self, cfg: JammerConfig):
        n = cfg.network
        self.cfg = cfg
        self.conn = JsonLineConnection(n.host, n.rl_port, n.rl_timeout_sec,
                                       name="rl")
        self.last_connect_attempt = 0.0

    def connect(self) -> bool:
        self.last_connect_attempt = time.monotonic()
        return self.conn.connect()

    def close(self) -> None:
        self.conn.close()

    def request_action(self, seq: int, features: np.ndarray, prev_action: dict,
                       tx_state: str) -> tuple[dict, dict]:
        """Return ``(action, rl_meta)``.  Never raises: falls back on any error."""
        if not self.conn.connected and not self.connect():
            return dict(prev_action), _fallback_meta("rl service unreachable")

        packet = {
            "type": "state_req",
            "version": "2.0",
            "session_id": self.cfg.network.session_id,
            "seq": int(seq),
            "timestamp_ms": int(time.time() * 1000),
            "state": {
                "task": TASK_NAME,
                "num_channels": int(self.cfg.spectrum.num_channels),
                "channel_features": np.asarray(features, dtype=float).tolist(),
                "prev_action": {
                    "channel_idx": int(prev_action["channel_idx"]),
                    "power_db": float(prev_action["power_db"]),
                    "bw_mhz": int(prev_action["bw_mhz"]),
                    "waveform_mode": int(prev_action["waveform_mode"]),
                },
                "tx_state": str(tx_state),
            },
        }

        try:
            resp = self.conn.request(packet, timeout=self.cfg.network.rl_timeout_sec)
        except (LineClosed, LineTimeout, OSError) as exc:
            self.reset()
            return dict(prev_action), _fallback_meta(f"rl request failed ({exc})")

        if resp.get("status") != "ok" or not isinstance(resp.get("action"), dict):
            return dict(prev_action), _fallback_meta("rl service returned a non-ok response")

        action = self.cfg.actions.coerce(
            resp["action"].get("channel_idx", prev_action["channel_idx"]),
            resp["action"].get("power_db", prev_action["power_db"]),
            resp["action"].get("bw_mhz", prev_action["bw_mhz"]),
            resp["action"].get("waveform_mode", prev_action["waveform_mode"]),
            self.cfg.spectrum.num_channels,
        )
        meta = dict(resp.get("rl_meta") or {})
        meta.setdefault("policy", "unknown")
        meta.setdefault("latency_ms", 0)
        return action, meta

    def reset(self) -> None:
        """Drop the socket; the next frame will try to reconnect."""
        self.conn.close()


def _fallback_meta(reason: str) -> dict:
    return {"policy": "fallback", "value": 0, "latency_ms": 0, "reason": reason}
