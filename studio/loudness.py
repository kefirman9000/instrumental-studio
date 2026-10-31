"""Integrated loudness (LUFS), as streaming services measure it (ITU-R BS.1770 / EBU R128):
K-weighting (a high shelf and a low cut), 400 ms blocks with 75% overlap, an absolute gate at -70 LUFS and
a relative gate 10 LU under the ungated level."""
import numpy as np


def _biquad_shelf(sr):
    # high shelf, +4 dB above ~1.5 kHz (the head's effect), from the analog prototype in BS.1770
    G, Q, fc = 3.999843853973347, 0.7071752369554196, 1681.974450955533
    K = np.tan(np.pi * fc / sr)
    Vh, Vb = 10 ** (G / 20), (10 ** (G / 20)) ** 0.4996667741545416
    a0 = 1 + K / Q + K * K
    b = [(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0]
    a = [1, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
    return b, a


def _biquad_highpass(sr):
    Q, fc = 0.5003270373238773, 38.13547087602444
    K = np.tan(np.pi * fc / sr)
    a0 = 1 + K / Q + K * K
    return [1, -2, 1], [1, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]


def lufs(x, sr):
    """Integrated loudness of (channels, n) audio, in LUFS (-inf for silence)."""
    from scipy.signal import lfilter
    x = np.atleast_2d(np.asarray(x, np.float64))
    b1, a1 = _biquad_shelf(sr)
    b2, a2 = _biquad_highpass(sr)
    y = lfilter(b2, a2, lfilter(b1, a1, x, axis=1), axis=1)
    blk, hop = int(0.4 * sr), int(0.1 * sr)
    if y.shape[1] < blk:
        return float("-inf")
    cs = np.concatenate([np.zeros((y.shape[0], 1)), np.cumsum(y ** 2, axis=1)], axis=1)
    starts = np.arange(0, y.shape[1] - blk + 1, hop)
    z = ((cs[:, starts + blk] - cs[:, starts]) / blk).sum(0)          # channel weights 1.0 (L, R)
    L = -0.691 + 10 * np.log10(z + 1e-20)
    z = z[L > -70]
    if not len(z):
        return float("-inf")
    rel = -0.691 + 10 * np.log10(z.mean()) - 10
    z2 = z[-0.691 + 10 * np.log10(z) > rel]
    return float(-0.691 + 10 * np.log10((z2 if len(z2) else z).mean()))
