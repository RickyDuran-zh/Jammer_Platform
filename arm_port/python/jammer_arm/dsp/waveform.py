"""Waveform generation: a faithful port of ``matlab/waveform/generate_jammer_signal.m``.

Two waveform modes are supported and both are defined by the original MATLAB code:

* ``waveform_mode = 0`` - band-limited complex Gaussian noise, shifted to the
  target channel with a complex exponential.
* ``waveform_mode = 1`` - four equal-spaced complex tones inside the requested
  bandwidth.

Everything is normalised to a conservative complex baseband peak
(``radio.tx_baseband_peak``, default 0.25).  ``power_db`` maps onto hardware TX
gain, **not** onto on/off state: ``power_db = 0`` is minimum gain, and the frame
is still transmitted whenever the RF interlock is armed.

FIR design note
---------------
MATLAB uses ``fir1(128, Wn, 'low')``: 129 taps, Hamming window, unity DC gain.
``scipy.signal.firwin(129, Wn)`` gives the same numbers, but scipy is an extra
ARM dependency, so :func:`design_lowpass` implements the same three steps in
numpy (ideal sinc, Hamming window, DC normalisation).  ``tests/test_equivalence``
compares the taps against the values exported from MATLAB.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

from ..config import JammerConfig

FIR_NUMTAPS = 129          # fir1(128, ...) -> 129 coefficients
TONE_COUNT = 4             # multi-tone mode uses four tones
MAX_CUTOFF = 0.95
MIN_CUTOFF = 0.001


def _sinc(x: np.ndarray) -> np.ndarray:
    """Normalised sinc: sin(pi x) / (pi x), with sinc(0) = 1."""
    out = np.ones_like(x, dtype=np.float64)
    nz = x != 0
    out[nz] = np.sin(np.pi * x[nz]) / (np.pi * x[nz])
    return out


@lru_cache(maxsize=64)
def design_lowpass(numtaps: int, cutoff_norm: float) -> np.ndarray:
    """MATLAB ``fir1(numtaps - 1, cutoff_norm, 'low')`` for a symmetric odd filter.

    ``cutoff_norm`` is normalised to the Nyquist frequency, exactly like the
    ``Wn`` argument of ``fir1`` and ``firwin``.
    """
    if numtaps % 2 == 0:
        raise ValueError("only odd-length (type I) filters are supported")
    m = (numtaps - 1) / 2.0
    n = np.arange(numtaps, dtype=np.float64)
    fc = cutoff_norm / 2.0                      # cycles per sample
    ideal = 2.0 * fc * _sinc(2.0 * fc * (n - m))
    window = 0.54 - 0.46 * np.cos(2.0 * np.pi * n / (numtaps - 1))
    taps = ideal * window
    total = taps.sum()
    if abs(total) < 1e-12:
        raise ValueError("degenerate low-pass design (zero DC gain)")
    return taps / total


def effective_bandwidth_hz(bw_mhz: float, channel_offset_hz: float,
                           sample_rate_hz: float) -> float:
    """Clip the requested bandwidth so the shifted signal stays inside Nyquist.

    Mirrors ``bw_hz = min(bw_hz, max(0, 2 * (fs/2 - abs(f0))))``.
    """
    bw_hz = float(bw_mhz) * 1e6
    max_half_bw = sample_rate_hz / 2.0 - abs(channel_offset_hz)
    return float(min(bw_hz, max(0.0, 2.0 * max_half_bw)))


def tx_gain_for_power(power_db: float, cfg: JammerConfig) -> float:
    """``power_db`` -> hardware TX gain, clamped to the configured window."""
    r = cfg.radio
    return float(np.clip(r.tx_gain_base_db + float(power_db) * cfg.actions.power_to_gain,
                         r.tx_gain_min_db, r.tx_gain_max_db))


def generate_jammer_signal(action: dict, cfg: JammerConfig,
                           shaping: str = "fir",
                           rng: np.random.Generator | None = None) -> tuple[np.ndarray, float]:
    """Generate one jamming frame for a four-field action.

    Returns ``(samples, tx_gain_db)`` where ``samples`` is ``complex128`` of
    length ``cfg.radio.num_samples`` and ``max(abs(samples)) == tx_baseband_peak``.
    """
    r = cfg.radio
    n = int(r.num_samples)
    fs = float(r.sample_rate_hz)
    t = np.arange(n, dtype=np.float64) / fs

    centers = cfg.channel_centers_hz
    ch = int(np.clip(int(action["channel_idx"]), 0, centers.size - 1))
    power_db = float(np.clip(float(action["power_db"]),
                             cfg.actions.power_db_min, cfg.actions.power_db_max))
    bw_mhz = float(action["bw_mhz"])
    mode = int(action["waveform_mode"])
    if mode not in cfg.actions.waveform_modes:
        raise ValueError(f"unsupported waveform_mode {mode}")

    f0 = float(centers[ch] - r.center_freq_hz)          # baseband offset
    bw_hz = effective_bandwidth_hz(bw_mhz, f0, fs)
    gain = tx_gain_for_power(power_db, cfg)

    if mode == 0:
        gen = rng if rng is not None else np.random.default_rng()
        base = gen.standard_normal(n) + 1j * gen.standard_normal(n)
        shaping_lc = shaping.lower()
        if shaping_lc == "fir":
            cutoff = float(np.clip((bw_hz / 2.0) / (fs / 2.0), MIN_CUTOFF, MAX_CUTOFF))
            taps = design_lowpass(FIR_NUMTAPS, cutoff)
            shaped = np.convolve(taps, base)[:n]        # filter(b, 1, x)
        elif shaping_lc == "fft":
            shaped = _bandlimit_fft(base, bw_hz, fs)
        else:
            raise ValueError(f"unknown shaping '{shaping}' (use 'fir' or 'fft')")
        y = shaped * np.exp(1j * 2.0 * np.pi * f0 * t)
    else:
        offsets = np.linspace(-bw_hz / 2.0, bw_hz / 2.0, TONE_COUNT) if TONE_COUNT > 1 else np.zeros(1)
        y = np.zeros(n, dtype=np.complex128)
        for off in offsets:
            y += np.exp(1j * 2.0 * np.pi * (f0 + off) * t)
        y /= np.sqrt(TONE_COUNT)

    peak = float(np.max(np.abs(y))) if n else 0.0
    if peak < 1e-12:
        y = np.zeros(n, dtype=np.complex128)
    else:
        y = r.tx_baseband_peak * y / peak
    return y.astype(np.complex128), gain


def _bandlimit_fft(x: np.ndarray, bw_hz: float, fs: float) -> np.ndarray:
    """Brick-wall band limiting in the frequency domain (equivalent, cheaper)."""
    n = x.size
    spec = np.fft.fftshift(np.fft.fft(x))
    freqs = (np.arange(n) - n / 2.0) * (fs / n)
    spec[np.abs(freqs) > bw_hz / 2.0] = 0.0
    return np.fft.ifft(np.fft.ifftshift(spec))


def waveform_summary(y: np.ndarray, fs: float) -> dict:
    """Diagnostics used by telemetry, tests and the benchmark."""
    spec = np.abs(np.fft.fftshift(np.fft.fft(y)))
    power = spec ** 2
    total = power.sum() + 1e-30
    freqs = (np.arange(y.size) - y.size / 2.0) * (fs / y.size)
    cum = np.cumsum(power) / total
    lo = freqs[np.searchsorted(cum, 0.005)]
    hi = freqs[min(np.searchsorted(cum, 0.995), freqs.size - 1)]
    return {
        "peak": float(np.max(np.abs(y))),
        "rms": float(np.sqrt(np.mean(np.abs(y) ** 2))),
        "occupied_bw_hz": float(hi - lo),
        "active_bins": int(np.count_nonzero(power > 0.01 * power.max())),
    }
