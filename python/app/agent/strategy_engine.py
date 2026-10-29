"""
Strategy engine for the AI agent — generates jamming actions frame-by-frame
based on an active strategy and real-time telemetry data.

Tactics select a sequence of four-field actions; MATLAB executes each action.
The tactic is separate from waveform_mode (noise or multi-tone).
"""
import random
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
        self.last_channel = self.channels[0]

    def tick(self, **kwargs):
        ch = self.channels[self._idx % len(self.channels)]
        self.last_channel = ch
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


class RandomStrategy(_BaseStrategy):
    """Random channel hopping with a fixed dwell and no immediate repeat."""

    def __init__(self, channels=None, start_channel=0, dwell_frames=25,
                 power_db=10.0, bw_mhz=4, waveform_mode=0):
        self.channels = list(dict.fromkeys(
            channels if channels is not None else range(10)))
        if len(self.channels) < 2:
            raise ValueError("随机跳频至少需要两个不同信道")
        self.dwell_frames = max(1, int(dwell_frames))
        self.power_db = float(power_db)
        self.bw_mhz = int(bw_mhz)
        self.waveform_mode = int(waveform_mode)
        self.current_channel = (int(start_channel) if start_channel in self.channels
                                else self.channels[0])
        self.last_channel = self.current_channel
        self._frame = 0

    def tick(self, **kwargs):
        action = {
            "channel_idx": self.current_channel,
            "power_db": self.power_db,
            "bw_mhz": self.bw_mhz,
            "waveform_mode": self.waveform_mode,
        }
        self.last_channel = self.current_channel
        self._frame += 1
        if self._frame >= self.dwell_frames:
            self._frame = 0
            choices = [ch for ch in self.channels if ch != self.current_channel]
            if choices:
                self.current_channel = random.choice(choices)
        return action


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


class TargetTrackingStrategy(TrackBusiestStrategy):
    """Follow the most occupied observed channel with switch hysteresis.

    The first channel feature is occupancy. If no channel passes the threshold,
    hold the previous target. A small margin avoids switching on noise alone.
    """

    def __init__(self, channel_idx=0, power_db=10.0, bw_mhz=4,
                 waveform_mode=0, min_occupancy=0.15, switch_margin=0.05):
        super().__init__(power_db=power_db, bw_mhz=bw_mhz,
                         waveform_mode=waveform_mode, min_occupancy=min_occupancy)
        self.current_channel = int(channel_idx)
        self.switch_margin = float(switch_margin)

    def tick(self, channel_features=None, **kwargs):
        if not channel_features:
            return self._action(self.current_channel)
        occupied = [float(row[0]) if row else 0.0 for row in channel_features]
        best_ch = max(range(len(occupied)), key=occupied.__getitem__)
        if self.current_channel >= len(occupied):
            self.current_channel = best_ch
        current_occ = occupied[self.current_channel]
        if (occupied[best_ch] >= self.min_occupancy and
                (current_occ < self.min_occupancy or
                 occupied[best_ch] >= current_occ + self.switch_margin)):
            self.current_channel = best_ch
        return self._action(self.current_channel)


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


# ── Coerce (强制型) ────────────────────────────────────────────────

class CoerceStrategy(TrackBusiestStrategy):
    """Aggressive profile: favor occupied channels and higher gain/bandwidth."""

    def __init__(self, power_db=20.0, bw_mhz=8, waveform_mode=0, min_occupancy=0.10):
        super().__init__(power_db=power_db, bw_mhz=bw_mhz,
                         waveform_mode=waveform_mode, min_occupancy=min_occupancy)

    def tick(self, channel_features=None, rl_action=None, **kwargs):
        action = super().tick(channel_features=channel_features)
        if rl_action:
            action["channel_idx"] = int(rl_action["channel_idx"])
            action["power_db"] = min(self.power_db, max(self.power_db * 0.75,
                                                         float(rl_action["power_db"])))
            action["bw_mhz"] = min(self.bw_mhz, max(4, int(rl_action["bw_mhz"])))
        return action


# ── Energy Save (能量节约型) ───────────────────────────────────────

class EnergySaveStrategy(_BaseStrategy):
    """Low gain/narrow band profile; no occupancy requests minimum gain.

    The current action contract has no TX-off action. Even at zero gain the
    hardware still transmits when its independent RF interlock is enabled.
    """

    def __init__(self, power_db=5.0, bw_mhz=2, waveform_mode=0, min_occupancy=0.25):
        self.power_db = float(power_db)
        self.bw_mhz = int(bw_mhz)
        self.waveform_mode = int(waveform_mode)
        self.min_occupancy = float(min_occupancy)
        self.current_channel = 0

    def tick(self, channel_features=None, rl_action=None, **kwargs):
        if not channel_features:
            return {"channel_idx": 0, "power_db": 0,
                    "bw_mhz": self.bw_mhz, "waveform_mode": self.waveform_mode}
        best_ch = max(
            range(len(channel_features)),
            key=lambda i: float(channel_features[i][0]) if len(channel_features[i]) > 0 else 0,
        )
        if rl_action:
            proposed_ch = int(rl_action["channel_idx"])
            if 0 <= proposed_ch < len(channel_features) and \
                    float(channel_features[proposed_ch][0]) >= self.min_occupancy:
                best_ch = proposed_ch
        occ = float(channel_features[best_ch][0]) if len(channel_features[best_ch]) > 0 else 0
        power = 0 if occ < self.min_occupancy else self.power_db
        if rl_action and occ >= self.min_occupancy:
            power = min(self.power_db, float(rl_action["power_db"]))
        self.current_channel = best_ch
        return {
            "channel_idx": best_ch,
            "power_db": power,
            "bw_mhz": min(self.bw_mhz, int(rl_action["bw_mhz"])) if rl_action else self.bw_mhz,
            "waveform_mode": int(rl_action["waveform_mode"]) if rl_action else self.waveform_mode,
        }


# ── Deception (欺骗诱导型) ─────────────────────────────────────────

class DeceptionStrategy(TrackBusiestStrategy):
    """Prototype profile with changing channel/waveform choices.

    This is a rule approximation, not the paper's learned mode-transition
    policy or a demonstrated deception effect.
    """

    def __init__(self, power_db=12.0, bw_mhz=4, waveform_mode=1, min_occupancy=0.10):
        super().__init__(power_db=power_db, bw_mhz=bw_mhz,
                         waveform_mode=waveform_mode, min_occupancy=min_occupancy)
        self._step = 0

    def tick(self, channel_features=None, rl_action=None, **kwargs):
        action = super().tick(channel_features=channel_features)
        self._step += 1
        if rl_action:
            action["channel_idx"] = int(rl_action["channel_idx"])
            action["power_db"] = max(0, min(20, self.power_db +
                                            (float(rl_action["power_db"]) - 10) * 0.25))
            action["bw_mhz"] = int(rl_action["bw_mhz"])
        # Pattern varies the available waveform, not an additional RF mode.
        if self._step % 7 in (2, 5):
            action["waveform_mode"] = 1 - self.waveform_mode
        return action


# ── Engine ──────────────────────────────────────────────────────────

# 三种预设策略的默认参数（UI 控制策略候选框与策略类共用的单一事实来源）
PRESET_STRATEGIES = {
    "coerce": {"power_db": 20, "bw_mhz": 8, "waveform_mode": 0, "min_occupancy": 0.10},
    "energy_save": {"power_db": 5, "bw_mhz": 2, "waveform_mode": 0, "min_occupancy": 0.25},
    "deception": {"power_db": 12, "bw_mhz": 4, "waveform_mode": 1, "min_occupancy": 0.10},
}

STRATEGY_REGISTRY = {
    "fixed": FixedStrategy,
    "sweep": SweepStrategy,
    "random": RandomStrategy,
    "target_track": TargetTrackingStrategy,
    "track_busiest": TrackBusiestStrategy,
    "track_weakest": TrackWeakestStrategy,
    "alternating": AlternatingStrategy,
    "reactive": ReactiveStrategy,
    "coerce": CoerceStrategy,
    "energy_save": EnergySaveStrategy,
    "deception": DeceptionStrategy,
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
             snr=0.0, bw_est=0.0, rl_action=None) -> dict | None:
        """Called each telemetry frame. Returns action dict or None."""
        with self._lock:
            if not self._active or not self._strategy:
                return None
            return self._strategy.tick(
                channel_features=channel_features,
                channel_map=channel_map,
                snr=snr,
                bw_est=bw_est,
                rl_action=rl_action,
            )
