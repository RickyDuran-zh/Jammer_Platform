"""
Command bridge: TCP server that accepts commands from the UI agent and
forwards them to the MATLAB backend via a polling mechanism.

MATLAB connects as a client, sends a poll request, receives pending commands.
"""
import json
import socket
import threading
import time
from collections import deque
from typing import Optional


class CommandBridge(threading.Thread):
    """TCP server for sending control commands to MATLAB backend.

    Protocol:
      MATLAB sends:  {"type": "cmd_poll"}
      Server responds: {"type": "cmd_rsp", "commands": [...], "timestamp_ms": ...}
      (empty commands list if nothing pending)
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5557):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self._stop_event = threading.Event()
        self._commands: deque = deque()
        self._lock = threading.Lock()
        self._server: Optional[socket.socket] = None
        self._connected = threading.Event()
        self._bind_failed = False

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def bind_failed(self) -> bool:
        return self._bind_failed

    def enqueue_command(self, cmd: dict):
        """Enqueue a command to be sent to MATLAB on next poll."""
        with self._lock:
            self._commands.append(cmd)

    def enqueue_control_mode(self, mode: str):
        """Enqueue a control-mode switch command."""
        self.enqueue_command({"type": "set_control_mode", "mode": mode})

    def enqueue_action(self, action: dict):
        """Enqueue a full jamming action (all 4 params)."""
        self.enqueue_command(action)

    def stop(self):
        self._stop_event.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass

    def run(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._server.bind((self.host, self.port))
        except OSError:
            print(f"[CmdBridge] failed to bind {self.host}:{self.port} — port may be in use")
            self._bind_failed = True
            return
        self._server.listen(1)
        self._server.settimeout(1.0)
        print(f"[CmdBridge] listening on {self.host}:{self.port}")

        while not self._stop_event.is_set():
            try:
                conn, addr = self._server.accept()
                conn.settimeout(1.0)
            except (socket.timeout, OSError):
                continue

            self._connected.set()
            print(f"[CmdBridge] MATLAB connected from {addr}")
            with conn:
                buffer = b""
                while not self._stop_event.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        break

                    if not chunk:
                        break

                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            pkt = json.loads(line.decode("utf-8"))
                        except json.JSONDecodeError:
                            continue

                        if pkt.get("type") == "cmd_poll":
                            with self._lock:
                                cmds = list(self._commands)
                                self._commands.clear()

                            rsp = {
                                "type": "cmd_rsp",
                                "commands": cmds,
                                "timestamp_ms": int(time.time() * 1000),
                            }
                            try:
                                conn.sendall((json.dumps(rsp) + "\n").encode("utf-8"))
                            except OSError:
                                break

            self._connected.clear()
            print("[CmdBridge] MATLAB disconnected")

        if self._server:
            self._server.close()
        print("[CmdBridge] stopped")
