"""
Minimal client for the Python RL service (127.0.0.1:6001).

Lets the UI Agent request the running decision algorithm (PPO/A2C/BDQ)
for preset decisions or a one-shot tool call. The UI enqueues the result to MATLAB.

Protocol matches rl_server.py: send a JSON Lines ``state_req``, read one
``action_rsp``. All failures (service down, timeout, malformed response) return
``None`` so callers can degrade gracefully instead of raising.
"""
import json
import socket
import time


def request_rl_action(channel_features, prev_action=None, num_channels=10,
                      host="127.0.0.1", port=6001, timeout=2.0):
    """Ask the RL service for one action.

    Returns ``(action, rl_meta)`` on success, or ``None`` on any failure.
    """
    state = {
        "task": "jammer_spectrum_ctrl",
        "num_channels": int(num_channels),
        "channel_features": channel_features or [],
        "prev_action": prev_action or {},
        "tx_state": "agent",
    }
    pkt = {
        "type": "state_req",
        "version": "2.0",
        "session_id": "ui-agent",
        "seq": int(time.time() * 1000),
        "timestamp_ms": int(time.time() * 1000),
        "state": state,
    }

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(pkt) + "\n").encode("utf-8"))

            line = b""
            while b"\n" not in line:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                line += chunk

            rsp = json.loads(line.strip().decode("utf-8"))
            if (rsp.get("type") != "action_rsp" or rsp.get("status") != "ok"
                    or rsp.get("seq") != pkt["seq"]):
                return None
            action = rsp.get("action")
            if not isinstance(action, dict):
                return None
            if not (0 <= int(action["channel_idx"]) < num_channels
                    and 0 <= float(action["power_db"]) <= 20
                    and int(action["bw_mhz"]) in (2, 4, 6, 8)
                    and int(action["waveform_mode"]) in (0, 1)):
                return None
            meta = rsp.get("rl_meta", {})
            if not isinstance(meta, dict):
                return None
            return action, meta
    except Exception:
        return None
