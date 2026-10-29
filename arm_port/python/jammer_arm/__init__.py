"""ARM (RK3588) port of the jammer subsystem.

Pure-Python replacement for the MATLAB backend: USRP X310 via UHD, the same
10-channel sensing pipeline, the same four-field action space, the same three
JSON-Lines links (:6001 RL, :5555 telemetry, :5557 commands) and the same RF
interlock semantics.  The existing PyQt UI, RL service and Agent tooling run
against this backend unchanged.
"""

from .config import ConfigError, JammerConfig
from .version import __version__

__all__ = ["JammerConfig", "ConfigError", "__version__"]
