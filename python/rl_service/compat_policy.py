import numpy as np


class CompatSpectrumPolicy:
    def __init__(self, num_channels=10):
        self.num_channels = num_channels

    def select_action(self, state: dict):
        feats = state.get("channel_features", [])
        if not feats:
            print("[compat_policy] WARNING: empty channel_features, falling back to default action")
            return {
                "channel_idx": 0,
                "power_db": 5.0,
                "bw_mhz": 4,
                "waveform_mode": 0,
            }, {"policy": "compat_default"}

        n = min(len(feats), self.num_channels)
        best_idx = 0
        best_score = -1e9
        for i in range(n):
            f = feats[i]
            occ, interf, util, snr_like = [float(x) for x in f[:4]]
            score = 1.4 * occ + 1.2 * util + 0.6 * snr_like - 0.8 * interf
            if score > best_score:
                best_idx = i
                best_score = score

        power_db = np.clip(5.0 + 10.0 * max(0.0, min(1.0, best_score)), 0.0, 20.0)
        bw_mhz = 4 if best_score < 1.0 else 6
        waveform_mode = 0 if best_score < 1.1 else 1

        return {
            "channel_idx": int(best_idx),
            "power_db": float(power_db),
            "bw_mhz": int(bw_mhz),
            "waveform_mode": int(waveform_mode),
        }, {"policy": "compat_rule"}
