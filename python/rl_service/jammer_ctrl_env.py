import numpy as np

from action_codec import DiscreteActionCodec


class JammerSpectrumCtrlEnv:
    """Spectrum jamming environment with temporal correlation and user patterns.

    State transitions are now Markovian: each channel's occupancy drifts smoothly
    with a random walk component, simulating realistic spectrum dynamics.
    Periodic "user activity" patterns simulate real communication behavior.
    """

    def __init__(self, num_channels=10, episode_len=50, seed=None):
        self.num_channels = num_channels
        self.episode_len = episode_len
        self.rng = np.random.default_rng(seed)
        self.codec = DiscreteActionCodec(num_channels=num_channels)
        self.prev_action = {
            "channel_idx": 0, "power_db": 0.0, "bw_mhz": 4, "waveform_mode": 0,
        }
        self.t = 0
        # Internal state for temporal smoothing
        self._raw_occ = np.zeros(num_channels)
        self._raw_interf = np.zeros(num_channels)

    @property
    def obs_dim(self):
        return self.num_channels * 4 + 4

    @property
    def action_dim(self):
        return self.codec.action_dim

    def reset(self):
        self.t = 0
        self.prev_action = {"channel_idx": 0, "power_db": 0.0, "bw_mhz": 4, "waveform_mode": 0}
        self._raw_occ = self.rng.random(self.num_channels) * 0.3
        self._raw_interf = self.rng.random(self.num_channels) * 0.2
        self._sample_state()
        return self._get_obs()

    def _sample_state(self):
        # Temporal smoothing: random walk with mean reversion
        alpha = 0.15  # smoothing coefficient (lower = smoother)
        occ_noise = self.rng.normal(0, 0.12, self.num_channels)
        interf_noise = self.rng.normal(0, 0.08, self.num_channels)

        # Active user pattern: 2-3 channels are "active" at any time, cycling slowly
        active_period = 80  # frames per cycle
        phase = (self.t % active_period) / active_period
        active_base = np.zeros(self.num_channels)
        # Create 3 "user groups" that alternate
        for g in range(3):
            center = int((g * self.num_channels / 3 + phase * (self.num_channels / 3)) % self.num_channels)
            active_base[center] += 0.4
            active_base[(center + 1) % self.num_channels] += 0.25

        self._raw_occ = np.clip(
            (1 - alpha) * self._raw_occ + alpha * (
                0.25 + 0.65 * self.rng.random(self.num_channels) + active_base
            ) + occ_noise,
            0.0, 1.0
        )
        self._raw_interf = np.clip(
            (1 - alpha) * self._raw_interf + alpha * (
                0.15 + 0.6 * self.rng.random(self.num_channels)
            ) + interf_noise,
            0.0, 1.0
        )

        self.occupancy = np.clip(self._raw_occ, 0.0, 1.0)
        self.interference = np.clip(self._raw_interf, 0.0, 1.0)
        self.utility = np.clip(
            0.3 + 0.7 * self.occupancy - 0.3 * self.interference + 0.1 * self.rng.random(self.num_channels),
            0.0, 1.0
        )
        # snr_like is now an independent feature, not derived from occ/interf
        self.snr_like = np.clip(
            0.3 * self.occupancy + 0.5 * self.rng.random(self.num_channels) + 0.2,
            0.0, 1.0
        )

    def step_discrete(self, action_idx: int):
        action = self.codec.decode(action_idx)
        return self.step_action(action)

    def step_continuous(self, action_vec):
        action = self.codec.project_continuous(action_vec)
        return self.step_action(action)

    def step_action(self, action: dict):
        ch = int(action["channel_idx"])
        power_db = float(action["power_db"])
        bw_mhz = int(action["bw_mhz"])
        waveform_mode = int(action["waveform_mode"])

        occ = float(self.occupancy[ch])
        interf = float(self.interference[ch])
        util = float(self.utility[ch])

        # Reward: reward hitting active/high-utility channels, penalize cost
        # Remove collinear snr_like — keep reward simple and interpretable
        hit_reward = 1.5 * occ + 1.2 * util
        power_cost = 0.04 * power_db
        bw_cost = 0.02 * max(0, bw_mhz - 4)
        interf_pen = 0.8 * interf

        # Switch cost for ALL parameter changes (not just channel)
        prev = self.prev_action
        switch_cost = (
            (0.12 if ch != int(prev["channel_idx"]) else 0.0)
            + 0.03 * abs(power_db - float(prev["power_db"]))
            + (0.03 if bw_mhz != int(prev["bw_mhz"]) else 0.0)
            + (0.03 if waveform_mode != int(prev["waveform_mode"]) else 0.0)
        )

        # Mode bonus: wider conditions so agent can discover it
        mode_bonus = 0.0
        if waveform_mode == 1 and bw_mhz >= 4 and occ > 0.3:
            mode_bonus = 0.12

        reward = hit_reward - power_cost - bw_cost - interf_pen - switch_cost + mode_bonus

        self.prev_action = dict(action)
        self.t += 1
        done = self.t >= self.episode_len

        self._sample_state()
        return self._get_obs(), float(reward), done, {}

    def _get_obs(self):
        obs = []
        for k in range(self.num_channels):
            obs.extend([
                float(self.occupancy[k]),
                float(self.interference[k]),
                float(self.utility[k]),
                float(self.snr_like[k]),
            ])
        obs.extend([
            float(self.prev_action["channel_idx"]) / max(self.num_channels - 1, 1),
            float(self.prev_action["power_db"]) / 20.0,
            (float(self.prev_action["bw_mhz"]) - 2.0) / 6.0,
            float(self.prev_action["waveform_mode"]),
        ])
        return np.array(obs, dtype=np.float32)
