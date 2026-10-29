"""Channel-grid sensing: a faithful port of ``matlab/sensing/sense_channel_grid.m``.

The port is intentionally a line-by-line translation instead of a "better" DSP
pipeline, because the RL checkpoints were trained on the feature vector this
function produces.  Any behavioural change (different normalisation, different
``std`` degrees of freedom, different channel binning) silently invalidates the
policy.  ``arm_port/tests/test_equivalence.py`` compares this implementation
against golden vectors exported from the MATLAB original.

Feature vector per channel: ``[occupancy, interference, utility, snr_like]``.
Channel map: 0 = idle, 1 = user occupied, 2 = jammed only, 3 = occupied + jam overlap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..config import JammerConfig

#: ``sig_type`` thresholds from the MATLAB implementation.
SIG_TYPE_NARROW_HZ = 20e6
SIG_TYPE_MEDIUM_HZ = 50e6

#: Floor used when the frame is completely silent (MATLAB would return -Inf).
SILENCE_SNR_DB = -120.0


@dataclass
class SensingResult:
    """Output of one sensing frame."""

    channel_features: np.ndarray = field(default_factory=lambda: np.zeros((10, 4)))
    channel_map: np.ndarray = field(default_factory=lambda: np.zeros(10, dtype=np.int64))
    snr_est: float = 0.0
    bw_est: float = 0.0
    sig_type: int = 0
    spectrum_abs: np.ndarray = field(default_factory=lambda: np.zeros(0))
    freq_axis_hz: np.ndarray = field(default_factory=lambda: np.zeros(0))

    def copy(self) -> "SensingResult":
        return SensingResult(
            channel_features=self.channel_features.copy(),
            channel_map=self.channel_map.copy(),
            snr_est=self.snr_est,
            bw_est=self.bw_est,
            sig_type=self.sig_type,
            spectrum_abs=self.spectrum_abs.copy(),
            freq_axis_hz=self.freq_axis_hz.copy(),
        )

    @classmethod
    def empty(cls, cfg: JammerConfig) -> "SensingResult":
        return cls(
            channel_features=np.zeros((cfg.spectrum.num_channels, 4)),
            channel_map=np.zeros(cfg.spectrum.num_channels, dtype=np.int64),
        )


def spectrum_abs(rx: np.ndarray, n_fft: int | None = None) -> np.ndarray:
    """``abs(fftshift(fft(rx, N)))`` with ``N = max(4096, len(rx))``."""
    n = max(4096, rx.size) if n_fft is None else n_fft
    return np.abs(np.fft.fftshift(np.fft.fft(rx, n)))


def freq_axis_hz(center_freq_hz: float, sample_rate_hz: float, n: int) -> np.ndarray:
    """``fc + (-N/2:N/2-1) * fs/N`` without relying on float colon semantics."""
    return center_freq_hz + (np.arange(n) - n / 2.0) * (sample_rate_hz / n)


def sense_channel_grid(rx_signal: np.ndarray, cfg: JammerConfig,
                       prev_action: dict | None = None) -> SensingResult:
    """Slice the received spectrum into the 10 logical channels."""
    rx = np.asarray(rx_signal, dtype=np.complex128).ravel()
    n = max(4096, rx.size)
    spec = np.fft.fftshift(np.fft.fft(rx, n))
    power_spec = np.abs(spec) ** 2
    power_norm = power_spec / (power_spec.max() + 1e-12)

    # ── global coarse metrics (UI compatibility) ─────────────────────
    signal_region = power_norm > 0.3
    noise_region = power_norm < 0.3
    n_sig = int(np.count_nonzero(signal_region))
    n_noise = int(np.count_nonzero(noise_region))

    signal_power = power_spec[signal_region].mean() if n_sig >= 10 else power_spec.mean()
    noise_power = power_spec[noise_region].mean() if n_noise >= 10 else power_spec.mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        snr_est = float(10.0 * np.log10(signal_power / (noise_power + 1e-12)))
    if not np.isfinite(snr_est):
        # All-zero input (no device / disconnected antenna).  MATLAB yields -Inf,
        # which cannot be put on the JSON wire, so clamp to a documented floor.
        snr_est = SILENCE_SNR_DB

    active_bins = np.flatnonzero(power_norm > 0.1)
    if active_bins.size == 0:
        bw_est = 0.0
        sig_type = 0
    else:
        bw_bins = int(active_bins[-1] - active_bins[0])
        bw_est = float(bw_bins * (cfg.radio.sample_rate_hz / n))
        if bw_est < SIG_TYPE_NARROW_HZ:
            sig_type = 1
        elif bw_est < SIG_TYPE_MEDIUM_HZ:
            sig_type = 2
        else:
            sig_type = 3

    f_axis = freq_axis_hz(cfg.radio.center_freq_hz, cfg.radio.sample_rate_hz, n)
    noise_floor = float(power_norm[noise_region].mean()) if n_noise > 0 else 0.0

    num_channels = cfg.spectrum.num_channels
    centers = cfg.channel_centers_hz
    half = cfg.spectrum.channel_width_hz / 2.0

    features = np.zeros((num_channels, 4), dtype=np.float64)
    ch_map = np.zeros(num_channels, dtype=np.int64)

    for k in range(num_channels):
        lo = np.searchsorted(f_axis, centers[k] - half, side="left")
        hi = np.searchsorted(f_axis, centers[k] + half, side="left")
        if hi <= lo:
            continue
        block = power_norm[lo:hi]
        ch_energy = float(block.mean())
        ch_peak = float(block.max())
        occ = min(1.0, ch_energy * 1.8)
        # MATLAB std() uses N-1 normalisation; np.std defaults to N.
        dev = float(block.std(ddof=1)) if block.size > 1 else 0.0
        interf = min(1.0, dev * 4.0 + 0.2 * ch_peak)
        util = max(0.0, min(1.0, 0.65 * occ + 0.35 * ch_peak - 0.25 * interf))
        snr_like = max(0.0, min(1.0, ch_peak - noise_floor))

        features[k, :] = (occ, interf, util, snr_like)

        if occ < 0.15:
            ch_map[k] = 0
        elif interf > 0.65:
            ch_map[k] = 2
        else:
            ch_map[k] = 1

    _annotate_jam_overlap(ch_map, prev_action, cfg)

    return SensingResult(
        channel_features=features,
        channel_map=ch_map,
        snr_est=snr_est,
        bw_est=bw_est,
        sig_type=sig_type,
        spectrum_abs=np.abs(spec),
        freq_axis_hz=f_axis,
    )


def _annotate_jam_overlap(ch_map: np.ndarray, prev_action: dict | None,
                          cfg: JammerConfig) -> None:
    """Mark channels covered by the previous action's *requested* bandwidth."""
    if not prev_action:
        return
    if "channel_idx" not in prev_action or "bw_mhz" not in prev_action:
        return

    centers = cfg.channel_centers_hz
    ch = int(np.clip(int(prev_action["channel_idx"]), 0, centers.size - 1))
    jam_center = centers[ch]
    jam_half_bw = float(prev_action["bw_mhz"]) * 1e6 / 2.0
    ch_half = cfg.spectrum.channel_width_hz / 2.0

    for k in range(centers.size):
        if abs(centers[k] - jam_center) < (jam_half_bw + ch_half):
            if ch_map[k] == 1:
                ch_map[k] = 3
            elif ch_map[k] == 0:
                ch_map[k] = 2
