import argparse
import json
import os
import signal
import socket
import sys
import time

import numpy as np

from compat_policy import CompatSpectrumPolicy
from jammer_ctrl_env import JammerSpectrumCtrlEnv
from ppo_agent import PPOAgent
from a2c_agent import A2CAgent

CHECKPOINT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints")
MAX_BUFFER = 2 * 1024 * 1024  # 2 MB per-connection buffer limit


class RLServer:
    def __init__(self, host="127.0.0.1", port=6001, algo="ppo"):
        self.host = host
        self.port = port
        self.algo = algo
        self._stop = False
        self.compat_policy = CompatSpectrumPolicy(num_channels=10)

        env = JammerSpectrumCtrlEnv()
        self.env_obs_dim = env.obs_dim
        self.env_action_dim = env.action_dim

        # Only instantiate the selected agent
        if self.algo == "bdq":
            from bdq_agent import BDQAgent
            self.agent = BDQAgent(self.env_obs_dim)
            self._load_checkpoint(self.agent, "bdq_spectrum_ctrl.pt", "BDQ")
        elif self.algo == "a2c":
            self.agent = A2CAgent(self.env_obs_dim, self.env_action_dim)
            self._load_checkpoint(self.agent, "a2c_spectrum_ctrl.pt", "A2C")
        else:
            self.agent = PPOAgent(self.env_obs_dim, self.env_action_dim)
            self._load_checkpoint(self.agent, "ppo_spectrum_ctrl.pt", "PPO")

    def _load_checkpoint(self, agent, filename, label):
        path = os.path.join(CHECKPOINT_DIR, filename)
        try:
            agent.load(path)
            print(f"[RL] loaded {label} checkpoint")
        except FileNotFoundError:
            print(f"[RL] {label} checkpoint not found at {path}, using random init")
        except Exception as e:
            print(f"[RL] failed to load {label} checkpoint from {path}: {e}, using random init")

    def build_obs(self, state: dict):
        feats = np.asarray(state.get("channel_features", []), dtype=np.float32)
        num_channels = int(state.get("num_channels", 10))
        if feats.ndim != 2 or feats.shape[0] != num_channels:
            return None
        prev_action = state.get("prev_action", {})
        obs = feats.reshape(-1).tolist()
        obs.extend([
            float(prev_action.get("channel_idx", 0)) / max(num_channels - 1, 1),
            float(prev_action.get("power_db", 0.0)) / 20.0,
            (float(prev_action.get("bw_mhz", 4.0)) - 2.0) / 6.0,
            float(prev_action.get("waveform_mode", 0)),
        ])
        return np.array(obs, dtype=np.float32)

    def select_action(self, state: dict):
        obs = self.build_obs(state)
        if obs is None:
            return self.compat_policy.select_action(state)

        if self.algo == "bdq":
            action, value = self.agent.act(obs, deterministic=True)
            meta = {"policy": "bdq", "value": value}
            return action, meta
        elif self.algo == "a2c":
            action, _, _, value = self.agent.act(obs, deterministic=True)
            meta = {"policy": "a2c", "value": value}
            return action, meta
        else:
            action, _, _, value = self.agent.act(obs, deterministic=True)
            meta = {"policy": "ppo", "value": value}
            return action, meta

    def serve_forever(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(1)
        server.settimeout(1.0)

        print(f"[RL] listening on {self.host}:{self.port}, algo={self.algo}")

        while not self._stop:
            try:
                conn, addr = server.accept()
                conn.settimeout(1.0)
            except socket.timeout:
                continue

            print(f"[RL] connected by {addr}")
            with conn:
                buffer = b""
                while not self._stop:
                    try:
                        chunk = conn.recv(8192)
                    except socket.timeout:
                        continue
                    except OSError:
                        break

                    if not chunk:
                        break

                    buffer += chunk
                    if len(buffer) > MAX_BUFFER:
                        print("[RL] buffer overflow, disconnecting client")
                        break

                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            pkt = json.loads(line.decode("utf-8"))
                        except json.JSONDecodeError:
                            continue

                        if pkt.get("type") != "state_req":
                            continue

                        t0 = time.time()
                        session_id = pkt.get("session_id", "unknown")
                        seq = pkt.get("seq", -1)
                        state = pkt.get("state", {})

                        action, rl_meta = self.select_action(state)
                        rl_meta["latency_ms"] = int((time.time() - t0) * 1000)

                        rsp = {
                            "type": "action_rsp",
                            "version": "2.0",
                            "session_id": session_id,
                            "seq": seq,
                            "timestamp_ms": int(time.time() * 1000),
                            "status": "ok",
                            "action": action,
                            "rl_meta": rl_meta,
                        }
                        try:
                            conn.sendall((json.dumps(rsp) + "\n").encode("utf-8"))
                        except OSError:
                            break

        print("[RL] server stopped")
        server.close()

    def stop(self):
        self._stop = True


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6001)
    p.add_argument("--algo", choices=["ppo", "a2c", "bdq"], default="ppo")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    server = RLServer(host=args.host, port=args.port, algo=args.algo)

    def _on_signal(signum, frame):
        print(f"\n[RL] received signal {signum}, shutting down...")
        server.stop()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    server.serve_forever()
