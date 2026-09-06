"""
Strategy engine for the AI agent — generates jamming actions frame-by-frame
based on an active strategy and real-time telemetry data.

Strategies:
  fixed       — hold constant parameters
  sweep       — cycle through all channels sequentially
  track_busy  — always target the channel with highest occupancy
  track_weak  — always target the channel with lowest SNR-like metric
  alternating — alternate between N channels
  reactive    — auto-switch waveform mode based on SNR
"""
import threading
from typing import Optional


# ── Strategy base ──────────────────────────────────────────────────

class _BaseStrategy:
    """Abstract strategy. Subclasses override `tick()`."""

    def tick(self, channel_features: list, channel_map: list,
             snr: float, bw_est: float) -> dict | None:
        """Return next action dict, or None if strategy is inactive."""
        raise NotImplementedError

    def stop(self):
        pass


# ── Fixed ──────────────────────────────────────────────────────────

class FixedStrategy(_BaseStrategy):
    def __init__(self, channel_idx=0, power_db=10.0, bw_mhz=4, waveform_mode=0):
        self.action = {
            "channel_idx": int(channel_idx),
            "power_db": float(power_db),
            "bw_mhz": int(bw_mhz),
            "waveform_mode": int(waveform_mode),
        }

    def tick(self, **kwargs):
        return dict(self.action)


# ── Sweep ───────────────────────────────────────────────────────────

class SweepStrategy(_BaseStrategy):
    def __init__(self, channels=None, dwell_frames=25, power_db=10.0,
                 bw_mhz=4, waveform_mode=0):
        self.channels = channels or list(range(10))
        self.dwell_frames = max(1, int(dwell_frames))
        self.power_db = float(power_db)
        self.bw_mhz = int(bw_mhz)
        self.waveform_mode = int(waveform_mode)
        self._idx = 0
        self._frame = 0

    def tick(self, **kwargs):
        ch = self.channels[self._idx % len(self.channels)]
        self._frame += 1
        if self._frame >= self.dwell_frames:
            self._frame = 0
            self._idx += 1
        return {
            "channel_idx": ch,
            "power_db": self.power_db,
            "bw_mhz": self.bw_mhz,
            "waveform_mode": self.waveform_mode,
        }

    @property
    def current_channel(self):
        return self.channels[self._idx % len(self.channels)]


# ── Track Busiest ───────────────────────────────────────────────────

class TrackBusiestStrategy(_BaseStrategy):
    def __init__(self, power_db=10.0, bw_mhz=4, waveform_mode=0, min_occupancy=0.15):
        self.power_db = float(power_db)
        self.bw_mhz = int(bw_mhz)
        self.waveform_mode = int(waveform_mode)
        self.min_occupancy = float(min_occupancy)
        self.current_channel = 0

    def tick(self, channel_features=None, **kwargs):
        if not channel_features:
            return self._action(0)
        best_ch = max(
            range(len(channel_features)),
            key=lambda i: float(channel_features[i][0]) if len(channel_features[i]) > 0 else 0,
        )
        occ = float(channel_features[best_ch][0]) if len(channel_features[best_ch]) > 0 else 0
        if occ >= self.min_occupancy:
            self.current_channel = best_ch
        return self._action(self.current_channel)

    def _action(self, ch):
        return {
            "channel_idx": ch,
            "power_db": self.power_db,
            "bw_mhz": self.bw_mhz,
            "waveform_mode": self.waveform_mode,
        }


# ── Track Weakest (lowest SNR-like) ─────────────────────────────────

class TrackWeakestStrategy(_BaseStrategy):
    def __init__(self, power_db=15.0, bw_mhz=6, waveform_mode=0):
        self.power_db = float(power_db)
        self.bw_mhz = int(bw_mhz)
        self.waveform_mode = int(waveform_mode)

    def tick(self, channel_features=None, **kwargs):
        if not channel_features:
            return self._action(0)
        best_ch = min(
            range(len(channel_features)),
            key=lambda i: float(channel_features[i][3]) if len(channel_features[i]) > 3 else 999,
        )
        return self._action(best_ch)

    def _action(self, ch):
        return {"channel_idx": ch, "power_db": self.power_db,
                "bw_mhz": self.bw_mhz, "waveform_mode": self.waveform_mode}


# ── Alternating ─────────────────────────────────────────────────────

class AlternatingStrategy(_BaseStrategy):
    def __init__(self, channels=(0, 5), dwell_frames=30, power_db=10.0,
                 bw_mhz=4, waveform_mode=0):
        self.channels = list(channels)
        self.dwell_frames = max(1, int(dwell_frames))
        self.power_db = float(power_db)
        self.bw_mhz = int(bw_mhz)
        self.waveform_mode = int(waveform_mode)
        self._idx = 0
        self._frame = 0

    def tick(self, **kwargs):
        ch = self.channels[self._idx % len(self.channels)]
        self._frame += 1
        if self._frame >= self.dwell_frames:
            self._frame = 0
            self._idx += 1
        return {
            "channel_idx": ch, "power_db": self.power_db,
            "bw_mhz": self.bw_mhz, "waveform_mode": self.waveform_mode,
        }


# ── Reactive ────────────────────────────────────────────────────────

class ReactiveStrategy(_BaseStrategy):
    def __init__(self, channel_idx=0, noise_below_snr=10.0, tone_above_snr=15.0,
                 power_db=10.0, bw_mhz=4):
        self.channel_idx = int(channel_idx)
        self.noise_below = float(noise_below_snr)
        self.tone_above = float(tone_above_snr)
        self.power_db = float(power_db)
        self.bw_mhz = int(bw_mhz)

    def tick(self, snr=0, channel_features=None, **kwargs):
        # Check SNR on target channel
        ch_snr = 0
        if channel_features and len(channel_features) > self.channel_idx:
            f = channel_features[self.channel_idx]
            if len(f) > 3:
                ch_snr = float(f[3]) * 20  # scale snr_like(0-1) to dB-ish

        if snr > 0:
            ch_snr = max(ch_snr, snr)

        mode = 0  # default noise
        if ch_snr >= self.tone_above:
            mode = 1  # multi-tone for strong signals
        elif ch_snr < self.noise_below:
            mode = 0  # noise for weak signals

        return {
            "channel_idx": self.channel_idx,
            "power_db": self.power_db,
            "bw_mhz": self.bw_mhz,
            "waveform_mode": mode,
        }


# ── Engine ──────────────────────────────────────────────────────────

STRATEGY_REGISTRY = {
    "fixed": FixedStrategy,
    "sweep": SweepStrategy,
    "track_busiest": TrackBusiestStrategy,
    "track_weakest": TrackWeakestStrategy,
    "alternating": AlternatingStrategy,
    "reactive": ReactiveStrategy,
}


class StrategyEngine:
    """Manages the active jamming strategy. Thread-safe for UI integration."""

    def __init__(self):
        self._lock = threading.Lock()
        self._strategy: Optional[_BaseStrategy] = None
        self._strategy_name: str = ""
        self._active = False

    # ── Public API ─────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    @property
    def strategy_name(self) -> str:
        return self._strategy_name

    def start_strategy(self, name: str, params: dict | None = None):
        """Start a named strategy with optional parameters."""
        params = params or {}
        cls = STRATEGY_REGISTRY.get(name)
        if cls is None:
            raise ValueError(
                f"未知策略: {name}。可用: {', '.join(STRATEGY_REGISTRY.keys())}"
            )
        with self._lock:
            if self._strategy:
                self._strategy.stop()
            self._strategy = cls(**params)
            self._strategy_name = name
            self._active = True

    def stop_strategy(self):
        """Stop the current strategy."""
        with self._lock:
            if self._strategy:
                self._strategy.stop()
            self._strategy = None
            self._strategy_name = ""
            self._active = False

    def tick(self, channel_features=None, channel_map=None,
             snr=0.0, bw_est=0.0) -> dict | None:
        """Called each telemetry frame. Returns action dict or None."""
        with self._lock:
            if not self._active or not self._strategy:
                return None
            return self._strategy.tick(
                channel_features=channel_features,
                channel_map=channel_map,
                snr=snr,
                bw_est=bw_est,
            )
