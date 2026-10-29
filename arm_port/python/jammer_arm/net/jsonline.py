"""Newline-delimited JSON transport (the platform's only wire format).

All three links (RL :6001, telemetry :5555, command :5557) use UTF-8 JSON with one
object per line, exactly like the MATLAB backend.  This module provides the small
amount of framing, timeout and reconnection logic the port needs.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Callable

MAX_LINE_BYTES = 8 * 1024 * 1024


class LineClosed(ConnectionError):
    """Peer closed the connection."""


class LineTimeout(TimeoutError):
    """No complete line arrived before the deadline."""


class JsonLineConnection:
    """A request/response JSON-Lines client socket with lazy reconnection."""

    def __init__(self, host: str, port: int, timeout: float = 1.0,
                 connect_timeout: float = 2.0, name: str = "jsonline"):
        self.host = host
        self.port = port
        self.timeout = float(timeout)
        self.connect_timeout = float(connect_timeout)
        self.name = name
        self._sock: socket.socket | None = None
        self._buf = b""
        self._lock = threading.Lock()
        self.connect_attempts = 0
        self.last_error: str = ""

    # ── connection management ───────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> bool:
        """Try once to establish the connection.  Never raises."""
        with self._lock:
            if self._sock is not None:
                return True
            self.connect_attempts += 1
            try:
                sock = socket.create_connection((self.host, self.port),
                                                timeout=self.connect_timeout)
                sock.settimeout(self.timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock = sock
                self._buf = b""
                self.last_error = ""
                return True
            except OSError as exc:
                self.last_error = str(exc)
                self._sock = None
                return False

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
            self._sock = None
            self._buf = b""

    def __enter__(self) -> "JsonLineConnection":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ── io ──────────────────────────────────────────────────────────
    def send(self, payload: dict) -> None:
        if self._sock is None:
            raise LineClosed(f"{self.name}: not connected")
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self._sock.sendall(data)
        except OSError as exc:
            self.close()
            raise LineClosed(f"{self.name}: send failed ({exc})") from exc

    def recv(self, timeout: float | None = None) -> dict:
        """Read one JSON object.  Raises LineTimeout / LineClosed."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            line = self._take_line()
            if line is not None:
                return _decode(line, self.name)
            if self._sock is None:
                raise LineClosed(f"{self.name}: not connected")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LineTimeout(f"{self.name}: timeout waiting for a line")
            self._sock.settimeout(min(remaining, 1.0))
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                self.close()
                raise LineClosed(f"{self.name}: recv failed ({exc})") from exc
            if not chunk:
                self.close()
                raise LineClosed(f"{self.name}: peer closed the connection")
            self._buf += chunk
            if len(self._buf) > MAX_LINE_BYTES:
                self.close()
                raise LineClosed(f"{self.name}: line buffer overflow")

    def _take_line(self) -> bytes | None:
        if b"\n" not in self._buf:
            return None
        line, self._buf = self._buf.split(b"\n", 1)
        return line.strip()

    def request(self, payload: dict, timeout: float | None = None) -> dict:
        self.send(payload)
        return self.recv(timeout)


def _decode(line: bytes, name: str) -> dict:
    if not line:
        raise LineClosed(f"{name}: empty line")
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LineClosed(f"{name}: malformed JSON line ({exc})") from exc
    if not isinstance(obj, dict):
        raise LineClosed(f"{name}: expected a JSON object, got {type(obj).__name__}")
    return obj


class LineServer:
    """Single-client JSON-Lines server.  Used by tests and the mock UI sink."""

    def __init__(self, host: str, port: int, handler: Callable[[dict], dict | None],
                 name: str = "server"):
        self.host = host
        self.port = port
        self.handler = handler
        self.name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: socket.socket | None = None
        self._conn: socket.socket | None = None
        self.received: list[dict] = []
        self.connections = 0

    def start(self) -> "LineServer":
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._run, name=f"{self.name}-srv", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            conn.settimeout(0.2)
            with conn:
                self._conn = conn
                buf = b""
                while not self._stop.is_set():
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        try:
                            pkt = json.loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        self.received.append(pkt)
                        reply = self.handler(pkt)
                        if reply is not None:
                            try:
                                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
                            except OSError:
                                break
                self._conn = None

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._conn, self._server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
