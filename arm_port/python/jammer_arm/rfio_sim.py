"""Reference RFIO server in Python (no UHD).

Implements exactly the protocol that ``radio/rfio.py`` speaks and that the C++
daemon in ``arm_port/cpp`` implements, so the RFIO transport can be developed and
regression-tested before the daemon is built on the board.

    python -m jammer_arm.rfio_sim --port 5601

It synthesises IQ (it is *not* a radio) and records TX bursts so a full
``--radio rfio`` pipeline can be exercised without hardware.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading

import numpy as np

from .config import JammerConfig


class RfioSimServer(threading.Thread):
    def __init__(self, host: str = "127.0.0.1", port: int = 5601,
                 cfg: JammerConfig | None = None, seed: int | None = None):
        super().__init__(daemon=True, name="rfio-sim")
        self.host = host
        self.port = port
        self.cfg = cfg or JammerConfig()
        self.rng = np.random.default_rng(seed)
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self.opened = False
        self.opts: dict = {}
        self.tx_log: list[dict] = []
        self.rx_count = 0
        self.fail_rx = 0

    # ── lifecycle ────────────────────────────────────────────────────
    def run(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(1)
        srv.settimeout(0.2)
        self._server = srv
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(2.0)
            with conn:
                try:
                    self._serve(conn)
                except (OSError, json.JSONDecodeError):
                    pass
        srv.close()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass

    # ── protocol ─────────────────────────────────────────────────────
    def _serve(self, conn: socket.socket) -> None:
        fh = conn.makefile("rb")
        while not self._stop.is_set():
            line = fh.readline()
            if not line:
                return
            req = json.loads(line.decode("utf-8"))
            cmd = req.get("cmd")
            if cmd == "open":
                self.opts = req
                self.opened = True
                self._reply(conn, {"status": "ok", "info": {
                    "rx_rate": req.get("sample_rate", 20e6),
                    "rx_freq": req.get("center_freq", 3e9),
                    "rx_gain": req.get("rx_gain", 15.0),
                    "tx_gain": req.get("tx_gain", 0.0),
                    "rx_antenna": req.get("rx_antenna", "TX/RX"),
                    "tx_antenna": req.get("tx_antenna", "TX/RX"),
                    "simulated": True,
                }})
            elif cmd == "rx":
                n = int(req.get("nsamples", 4096))
                if self.fail_rx > 0:
                    self.fail_rx -= 1
                    self._reply(conn, {"status": "error", "error": "timeout"})
                    continue
                if not self.opened:
                    self._reply(conn, {"status": "error", "error": "device not open"})
                    continue
                self.rx_count += 1
                iq = self._synthetic(n)
                self._reply(conn, {"status": "ok", "nsamples": n, "frame_id": self.rx_count})
                conn.sendall(iq.astype("<c8").tobytes())
            elif cmd == "tx":
                n = int(req.get("nsamples", 0))
                payload = self._read_exact(fh, n * 8)
                samples = np.frombuffer(payload, dtype="<f4").view(np.complex64)
                if not self.opened:
                    self._reply(conn, {"status": "error", "error": "device not open"})
                    continue
                self.tx_log.append({
                    "nsamples": n,
                    "gain": float(req.get("gain", 0.0)),
                    "peak": float(np.max(np.abs(samples))) if samples.size else 0.0,
                })
                self._reply(conn, {"status": "ok", "sent": n})
            elif cmd == "rebuild_rx":
                self._reply(conn, {"status": "ok"})
            elif cmd == "rebuild":
                self._reply(conn, {"status": "ok"})
            elif cmd == "status":
                self._reply(conn, {"status": "ok", "info": {
                    "opened": self.opened, "rx_count": self.rx_count,
                    "tx_count": len(self.tx_log)}})
            elif cmd == "close":
                self.opened = False
                self._reply(conn, {"status": "ok"})
            elif cmd == "shutdown":
                self._reply(conn, {"status": "ok"})
                self._stop.set()
                return
            else:
                self._reply(conn, {"status": "error", "error": f"unknown cmd {cmd!r}"})

    @staticmethod
    def _reply(conn: socket.socket, obj: dict) -> None:
        conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    @staticmethod
    def _read_exact(fh, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = fh.read(count - len(data))
            if not chunk:
                raise OSError("client closed mid-payload")
            data += chunk
        return data

    # ── synthetic IQ ─────────────────────────────────────────────────
    def _synthetic(self, n: int) -> np.ndarray:
        c = self.cfg
        t = np.arange(n) / c.radio.sample_rate_hz
        iq = 0.02 * (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
        centers = c.channel_centers_hz - c.radio.center_freq_hz
        for k, off in enumerate(centers):
            amp = 0.1 + 0.4 * float(self.rng.random())
            iq = iq + amp * np.exp(1j * (2 * np.pi * off * t + self.rng.uniform(0, 6.28)))
        return iq.astype(np.complex64)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="RFIO reference server (synthetic IQ, no UHD)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5601)
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args(argv)

    server = RfioSimServer(args.host, args.port, seed=args.seed)
    server.start()
    print(f"[rfio-sim] listening on {args.host}:{args.port} (Ctrl+C to stop)", flush=True)
    try:
        while server.is_alive():
            server.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
