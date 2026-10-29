"""Configuration for the ARM (RK3588) jammer backend.

This module is the single source of truth for radio, action-space, network and
safety parameters of the ARM port, exactly like ``matlab/backend/jammer_config.m``
is for the MATLAB backend.  ``arm_port/tests/test_config.py`` asserts that every
value still matches the MATLAB original, so the two backends cannot drift apart
silently.

Safety rule
-----------
Transmission can only be armed by the environment variable ``ZH_RF_TX_ENABLE``
set to ``LAB_ONLY`` in the process environment.  A configuration *file* can never
arm the interlock -- :meth:`JammerConfig.load` rejects any attempt to do so.
``power_db = 0`` means minimum TX gain; it does **not** stop transmission.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

INTERLOCK_ENV = "ZH_RF_TX_ENABLE"
INTERLOCK_VALUE = "LAB_ONLY"

#: Keys a configuration file may never set (they are safety-relevant or derived).
FORBIDDEN_OVERRIDE_KEYS = frozenset(
    {"tx_enabled", "interlock_env", "interlock_value", "tx_baseband_peak"}
)


class ConfigError(ValueError):
    """Raised when a configuration value violates the运行契约."""


# ──────────────────────────────────────────────────────────────────────
# Radio / RF
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RadioConfig:
    """USRP X310 and sampling configuration (mirrors jammer_config.m)."""

    device_args: str = "type=x300,addr=192.168.10.2"
    center_freq_hz: float = 3.0e9
    master_clock_rate_hz: float = 200e6
    #: 200e6 / 10 = 20 MS/s, identical to InterpolationFactor in MATLAB.
    sample_rate_hz: float = 20e6
    num_samples: int = 4096
    rx_antenna: str = "TX/RX"
    tx_antenna: str = "TX/RX"
    rx_gain_db: float = 15.0
    tx_gain_base_db: float = 0.0
    tx_gain_min_db: float = 0.0
    tx_gain_max_db: float = 10.0
    #: Conservative complex baseband peak; TX frames are normalised to this.
    tx_baseband_peak: float = 0.25
    clock_source: str = "internal"
    #: Time to wait after opening the device for PLL lock / calibration.
    settle_sec: float = 0.5
    #: Per-recv timeout handed to UHD.
    recv_timeout_sec: float = 1.0
    #: Number of identical frames pushed per TX call (MATLAB: cfg.tx_repeat).
    tx_repeat: int = 1
    #: Time-division duplex: stop the RX stream while a TX burst is on air.
    tdm: bool = True
    #: "burst" (default) requests exactly num_samples per frame with
    #: STREAM_MODE_NUM_SAMPS_AND_DONE - the only mode that cannot overflow at a
    #: ~12 fps frame rate.  "continuous" keeps the RX stream running and is kept
    #: for experiments that drain faster than the sample rate.
    rx_mode: str = "burst"

    def validate(self) -> None:
        if self.num_samples < 512:
            raise ConfigError("num_samples must be >= 512")
        if not 0 < self.sample_rate_hz <= self.master_clock_rate_hz:
            raise ConfigError("sample_rate_hz must be in (0, master_clock_rate_hz]")
        if self.tx_gain_min_db > self.tx_gain_base_db:
            raise ConfigError("tx_gain_base_db must be >= tx_gain_min_db")
        if self.tx_gain_max_db < self.tx_gain_min_db:
            raise ConfigError("tx_gain_max_db must be >= tx_gain_min_db")
        if not 0 < self.tx_baseband_peak <= 1.0:
            raise ConfigError("tx_baseband_peak must be in (0, 1]")
        if self.tx_repeat < 1:
            raise ConfigError("tx_repeat must be >= 1")
        if self.rx_mode not in ("burst", "continuous"):
            raise ConfigError("rx_mode must be 'burst' or 'continuous'")


# ──────────────────────────────────────────────────────────────────────
# Spectrum / action space
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpectrumConfig:
    """10 logical 2 MHz channels inside a 20 MHz observation span."""

    total_span_hz: float = 20e6
    num_channels: int = 10
    channel_width_hz: float = 2e6

    def channel_centers(self, center_freq_hz: float) -> np.ndarray:
        """MATLAB: linspace(fc - 9e6, fc + 9e6, 10)."""
        half = self.channel_width_hz * (self.num_channels - 1) / 2
        return np.linspace(center_freq_hz - half, center_freq_hz + half, self.num_channels)

    def validate(self) -> None:
        if self.num_channels < 1:
            raise ConfigError("num_channels must be >= 1")
        if self.total_span_hz < self.channel_width_hz * self.num_channels:
            raise ConfigError("total_span_hz is too small for the channel grid")


@dataclass(frozen=True)
class ActionSpace:
    """Four-field discrete action: channel, power, bandwidth, waveform."""

    power_levels_db: tuple[float, ...] = (0.0, 5.0, 10.0, 15.0, 20.0)
    power_db_min: float = 0.0
    power_db_max: float = 20.0
    bw_choices_mhz: tuple[int, ...] = (2, 4, 6, 8)
    waveform_modes: tuple[int, ...] = (0, 1)
    #: power_db -> TX gain mapping coefficient used by the waveform generator.
    power_to_gain: float = 0.5

    def validate(self) -> None:
        if self.power_db_min > self.power_db_max:
            raise ConfigError("power_db_min must be <= power_db_max")
        if not self.bw_choices_mhz:
            raise ConfigError("bw_choices_mhz must not be empty")
        if min(self.bw_choices_mhz) <= 0:
            raise ConfigError("bandwidth choices must be positive")

    def coerce(self, channel_idx: Any, power_db: Any, bw_mhz: Any, waveform_mode: Any,
               num_channels: int) -> dict:
        """Clamp an action into the legal action space (never raises on noise)."""
        ch = int(np.clip(round(float(channel_idx)), 0, num_channels - 1))
        p = float(np.clip(float(power_db), self.power_db_min, self.power_db_max))
        bw = int(bw_mhz)
        if bw not in self.bw_choices_mhz:
            bw = min(self.bw_choices_mhz, key=lambda c: abs(c - bw))
        mode = int(waveform_mode)
        if mode not in self.waveform_modes:
            mode = self.waveform_modes[0]
        return {"channel_idx": ch, "power_db": p, "bw_mhz": bw, "waveform_mode": mode}


# ──────────────────────────────────────────────────────────────────────
# Network
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NetworkConfig:
    """JSON-Lines peers.  Ports must stay identical to the MATLAB backend."""

    host: str = "127.0.0.1"
    rl_port: int = 6001
    rl_timeout_sec: float = 2.0
    telemetry_port: int = 5555
    telemetry_timeout_sec: float = 1.0
    command_port: int = 5557
    command_timeout_sec: float = 1.0
    reconnect_backoff_sec: float = 1.0
    session_id: str = "jammer_spectrum_ctrl_single_usrp"

    def validate(self) -> None:
        for name in ("rl_port", "telemetry_port", "command_port"):
            port = getattr(self, name)
            if not 1 <= port <= 65535:
                raise ConfigError(f"{name} out of range: {port}")


# ──────────────────────────────────────────────────────────────────────
# Loop / recovery
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LoopConfig:
    """Frame pacing and RX recovery policy (mirrors jammer_backend_loop.m)."""

    frame_period_sec: float = 0.08
    mock_frame_period_sec: float = 0.12
    rx_timeout_reinit_threshold: int = 3
    rx_reinit_pause_sec: float = 0.2
    full_radio_reinit_threshold: int = 2
    #: After this many consecutive hard failures the loop keeps running with
    #: cached sensing instead of aborting.
    status_write_interval_sec: float = 1.0

    def validate(self) -> None:
        if self.frame_period_sec <= 0:
            raise ConfigError("frame_period_sec must be > 0")
        if self.rx_timeout_reinit_threshold < 1:
            raise ConfigError("rx_timeout_reinit_threshold must be >= 1")


@dataclass(frozen=True)
class SafetyConfig:
    """RF interlock.  Only the process environment can arm transmission."""

    interlock_env: str = INTERLOCK_ENV
    interlock_value: str = INTERLOCK_VALUE

    def armed(self, env: Mapping[str, str] | None = None) -> bool:
        env = os.environ if env is None else env
        return env.get(self.interlock_env, "") == self.interlock_value


# ──────────────────────────────────────────────────────────────────────
# Aggregate
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JammerConfig:
    radio: RadioConfig = field(default_factory=RadioConfig)
    spectrum: SpectrumConfig = field(default_factory=SpectrumConfig)
    actions: ActionSpace = field(default_factory=ActionSpace)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    #: Extended telemetry (actual applied bandwidth/gain) for the ARM backend.
    emit_hw_actuals: bool = True

    # ── derived ──────────────────────────────────────────────────────
    @property
    def channel_centers_hz(self) -> np.ndarray:
        return self.spectrum.channel_centers(self.radio.center_freq_hz)

    @property
    def bin_width_hz(self) -> float:
        return self.radio.sample_rate_hz / self.radio.num_samples

    def tx_armed(self, env: Mapping[str, str] | None = None) -> bool:
        return self.safety.armed(env)

    # ── construction ─────────────────────────────────────────────────
    def validate(self) -> "JammerConfig":
        self.radio.validate()
        self.spectrum.validate()
        self.actions.validate()
        self.network.validate()
        self.loop.validate()
        nyquist = self.radio.sample_rate_hz / 2
        max_center_offset = float(np.max(np.abs(self.channel_centers_hz - self.radio.center_freq_hz)))
        if max_center_offset >= nyquist:
            raise ConfigError(
                "outermost channel centre falls outside the Nyquist band "
                f"({max_center_offset / 1e6:.2f} MHz >= {nyquist / 1e6:.2f} MHz)"
            )
        return self

    def as_dict(self) -> dict:
        d = asdict(self)
        d["channel_centers_hz"] = self.channel_centers_hz.tolist()
        d["tx_armed_env"] = self.tx_armed()
        return d

    def with_overrides(self, overrides: Mapping[str, Any]) -> "JammerConfig":
        """Apply dotted ``section.key`` overrides (``--set radio.num_samples=8192``)."""
        if not overrides:
            return self
        base = asdict(self)
        base.pop("safety", None)      # interlock is env-only, never data
        data = _deep_merge_plain(base, _expand_dotted(overrides))
        return JammerConfig.from_dict(data, base=self)

    # ── serialisation ────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: Mapping[str, Any], base: "JammerConfig | None" = None) -> "JammerConfig":
        base = base or cls()
        # Keys starting with "_" are comments / metadata and are ignored, so the
        # deployed JSON file can document itself.
        data = {k: v for k, v in dict(data).items() if not str(k).startswith("_")}

        if "safety" in data:
            raise ConfigError(
                "the 'safety' section cannot be configured; the RF interlock is "
                f"armed only by {INTERLOCK_ENV}={INTERLOCK_VALUE} in the process environment"
            )
        for key in data:
            if key in FORBIDDEN_OVERRIDE_KEYS:
                raise ConfigError(
                    f"'{key}' cannot be set from a configuration file; "
                    f"RF transmission is armed only by {INTERLOCK_ENV}={INTERLOCK_VALUE}"
                )
        sections: dict[str, Any] = {}
        for name in ("radio", "spectrum", "actions", "network", "loop"):
            if name in data:
                sections[name] = _build_section(getattr(base, name), data.pop(name))
        emit = data.pop("emit_hw_actuals", base.emit_hw_actuals)
        if data:
            raise ConfigError(f"unknown configuration keys: {sorted(data)}")
        return replace(base, emit_hw_actuals=bool(emit), **sections)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None,
             overrides: Mapping[str, Any] | None = None) -> "JammerConfig":
        """Load defaults, then an optional JSON file, then dotted overrides."""
        cfg = cls()
        if path:
            p = Path(path)
            if not p.is_file():
                raise ConfigError(f"configuration file not found: {p}")
            with p.open("r", encoding="utf-8") as fh:
                cfg = cls.from_dict(json.load(fh), base=cfg)
        if overrides:
            cfg = cfg.with_overrides(overrides)
        return cfg.validate()


def _build_section(current: Any, values: Mapping[str, Any]) -> Any:
    if not isinstance(values, Mapping):
        raise ConfigError(f"section {type(current).__name__} must be an object")
    known = set(current.__dataclass_fields__)  # type: ignore[attr-defined]
    unknown = set(values) - known
    if unknown:
        raise ConfigError(
            f"unknown keys in {type(current).__name__}: {sorted(unknown)}; known: {sorted(known)}"
        )
    clean: dict[str, Any] = {}
    for key, value in values.items():
        default = getattr(current, key)
        if isinstance(default, tuple) and isinstance(value, list):
            value = tuple(value)
        if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        clean[key] = value
    return replace(current, **clean)


def _expand_dotted(overrides: Mapping[str, Any]) -> dict:
    out: dict[str, Any] = {}
    for key, value in overrides.items():
        parts = key.split(".")
        node = out
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"override '{key}' conflicts with another override")
        node[parts[-1]] = value
    return out


def _deep_merge_plain(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge_plain(out[key], value)
        else:
            out[key] = value
    return out


def parse_set_args(pairs: Sequence[str]) -> dict:
    """Parse ``["radio.num_samples=8192", ...]`` into a nested override mapping."""
    flat: dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            raise ConfigError(f"--set expects key=value, got '{item}'")
        key, raw = item.split("=", 1)
        flat[key.strip()] = _auto_type(raw.strip())
    return _expand_dotted(flat)


def _auto_type(raw: str) -> Any:
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if raw.startswith("[") or raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return raw
