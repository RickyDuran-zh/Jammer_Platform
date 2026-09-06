import numpy as np


class DiscreteActionCodec:
    def __init__(self, num_channels=10, power_levels_db=None, bw_choices_mhz=None, waveform_modes=None):
        self.num_channels = num_channels
        self.power_levels_db = power_levels_db or [0, 5, 10, 15, 20]
        self.bw_choices_mhz = bw_choices_mhz or [2, 4, 6, 8]
        self.waveform_modes = waveform_modes or [0, 1]

        self.actions = []
        for ch in range(self.num_channels):
            for p in self.power_levels_db:
                for bw in self.bw_choices_mhz:
                    for m in self.waveform_modes:
                        self.actions.append({
                            "channel_idx": ch,
                            "power_db": float(p),
                            "bw_mhz": int(bw),
                            "waveform_mode": int(m),
                        })

    @property
    def action_dim(self):
        return len(self.actions)

    def decode(self, action_idx: int):
        action_idx = int(np.clip(action_idx, 0, self.action_dim - 1))
        return self.actions[action_idx]

    def decode_from_branches(self, ch_idx, p_idx, bw_idx, mode_idx):
        """Decode from per-branch indices (used by BDQ agent)."""
        ch = int(np.clip(ch_idx, 0, self.num_channels - 1))
        p_db = float(self.power_levels_db[int(np.clip(p_idx, 0, len(self.power_levels_db) - 1))])
        bw = int(self.bw_choices_mhz[int(np.clip(bw_idx, 0, len(self.bw_choices_mhz) - 1))])
        mode = int(self.waveform_modes[int(np.clip(mode_idx, 0, len(self.waveform_modes) - 1))])
        return {"channel_idx": ch, "power_db": p_db, "bw_mhz": bw, "waveform_mode": mode}

    def project_continuous(self, a):
        ch_norm, p_norm, bw_norm, mode_norm = a
        ch = int(np.clip(round(ch_norm * (self.num_channels - 1)), 0, self.num_channels - 1))
        p = float(np.clip(p_norm, 0.0, 1.0))
        power_db = float(p * 20.0)

        bw_choices = np.array(self.bw_choices_mhz, dtype=np.float32)
        bw_val = 2.0 + float(np.clip(bw_norm, 0.0, 1.0)) * 6.0
        bw_mhz = int(bw_choices[np.argmin(np.abs(bw_choices - bw_val))])

        waveform_mode = 1 if float(mode_norm) >= 0.5 else 0
        return {
            "channel_idx": ch,
            "power_db": power_db,
            "bw_mhz": bw_mhz,
            "waveform_mode": waveform_mode,
        }
