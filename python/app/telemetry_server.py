import json
import socket
import sys
import threading
from typing import Callable, Optional


class TelemetryServer(threading.Thread):
    """TCP server that receives telemetry from MATLAB backend.

    Accepts one persistent client at a time. When the client disconnects,
    goes back to listening for a new connection.
    """

    def __init__(self, host: str, port: int, on_packet: Callable[[dict], None]):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.on_packet = on_packet
        self._stop_event = threading.Event()
        self._server: Optional[socket.socket] = None
        self._conn: Optional[socket.socket] = None
        self._lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._conn is not None

    def stop(self):
        self._stop_event.set()
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except OSError:
                    pass
                self._conn = None
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass

    def run(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(1)
        self._server.settimeout(1.0)
        print(f"[Telemetry] listening on {self.host}:{self.port}")

        while not self._stop_event.is_set():
            # Accept a client
            try:
                sock, addr = self._server.accept()
                sock.settimeout(1.0)
            except (socket.timeout, OSError):
                continue

            print(f"[Telemetry] MATLAB connected from {addr}")
            with self._lock:
                self._conn = sock

            try:
                self._read_loop(sock)
            except Exception as e:
                print(f"[Telemetry] connection error: {e}")
            finally:
                with self._lock:
                    self._conn = None
                try:
                    sock.close()
                except OSError:
                    pass
                print("[Telemetry] MATLAB disconnected, waiting for reconnection...")

        print("[Telemetry] stopped")

    def _read_loop(self, sock: socket.socket):
        buffer = b""
        while not self._stop_event.is_set():
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                return

            if not chunk:
                return

            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    pkt = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    print(f"[Telemetry] JSON decode error, raw: {line[:120]}", file=sys.stderr)
                    continue

                if pkt.get("type") == "telemetry":
                    try:
                        self.on_packet(pkt)
                    except Exception as e:
                        import traceback
                        print(f"[Telemetry] callback error: {e}\n{traceback.format_exc()}", file=sys.stderr)
