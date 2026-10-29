"""Command poller: port of ``matlab/interface/cmd_bridge_poll.m``.

The PyQt UI keeps serving on :5557 and the backend keeps polling it, so the
control card, the manual panel, the Agent tools and the preset strategies all
work unchanged.  Only the client implementation moved from MATLAB's ``tcpclient``
to a plain socket.
"""

from __future__ import annotations

from ..config import JammerConfig
from .jsonline import JsonLineConnection, LineClosed, LineTimeout

#: The MATLAB client waits up to ``cmd_timeout`` (1 s) for a reply.  The ARM port
#: bounds that further so a stalled UI can never wreck the frame cadence; the UI
#: answers a poll in well under a millisecond on loopback.
MAX_POLL_WAIT_SEC = 0.25


class CommandClient:
    def __init__(self, cfg: JammerConfig):
        n = cfg.network
        self.cfg = cfg
        self.timeout = min(n.command_timeout_sec, MAX_POLL_WAIT_SEC)
        self.conn = JsonLineConnection(n.host, n.command_port, self.timeout, name="cmd")
        self.polls = 0
        self.commands_applied = 0
        self.last_error = ""

    def connect(self) -> bool:
        return self.conn.connect()

    def close(self) -> None:
        self.conn.close()

    @property
    def connected(self) -> bool:
        return self.conn.connected

    def poll(self) -> list[dict]:
        """Return pending commands.  Never raises; empty list on any problem."""
        if not self.conn.connected and not self.conn.connect():
            self.last_error = self.conn.last_error
            return []
        try:
            resp = self.conn.request({"type": "cmd_poll"}, timeout=self.timeout)
        except (LineClosed, LineTimeout, OSError) as exc:
            self.last_error = str(exc)
            self.conn.close()
            return []
        self.polls += 1
        if resp.get("type") != "cmd_rsp":
            return []
        cmds = resp.get("commands") or []
        if isinstance(cmds, dict):
            cmds = [cmds]
        out = [c for c in cmds if isinstance(c, dict)]
        self.commands_applied += len(out)
        return out
