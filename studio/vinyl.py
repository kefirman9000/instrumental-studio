"""Remove clicks and crackle from vinyl rips.

How it works (the standard approach in restoration tools):
1. Find them. Clicks and crackle are very short spikes of high-frequency energy that stand far above the
   music around them. The signal is high-passed, and a sample is suspect when it's many times louder than
   the typical level of the surrounding 50 ms. Real music (hi-hats, snare, consonants) is spread over many
   milliseconds, so runs longer than a few ms are left alone.
2. Rebuild them. Each damaged stretch (with a little margin) is filled in from the sound on both sides:
   an autoregressive model of the surrounding audio predicts what belongs there (least-squares AR
   interpolation, after Janssen et al.), so tones carry straight through instead of being smoothed over.
"""
import numpy as np

STRENGTHS = {
    # threshold (x local level), longest click kept as a click (s), AR order
    "light": (9.0, 0.002, 24),
    "normal": (7.0, 0.003, 28),
    "strong": (5.0, 0.004, 32),
}


def _local_scale(h, sr):
    """Typical level of the high-passed signal around each sample (robust: median of 50 ms blocks)."""
    blk = max(64, int(0.05 * sr))
    n = len(h)
    nb = max(1, n // blk)
    a = np.abs(h[:nb * blk]).reshape(nb, blk)
    med = np.median(a, axis=1) * 1.4826 + 1e-7
    centers = (np.arange(nb) + 0.5) * blk
    return np.interp(np.arange(n), centers, med).astype(np.float32)


def find_clicks(x, sr, strength="normal"):
    """Sample runs [(start, end), ...] that are clicks, for one channel."""
    from scipy import signal as sig
    k, max_len, _ = STRENGTHS[strength]
    sos = sig.butter(4, 2500, "highpass", fs=sr, output="sos")
    h = sig.sosfiltfilt(sos, x).astype(np.float32)
    hot = np.abs(h) > k * _local_scale(h, sr)
    if not hot.any():
        return []
    # join hits closer than 0.5 ms, then keep only short runs (clicks), with a 0.3 ms margin
    idx = np.flatnonzero(hot)
    gap = max(1, int(0.0005 * sr))
    breaks = np.flatnonzero(np.diff(idx) > gap)
    starts = np.r_[idx[0], idx[breaks + 1]]
    ends = np.r_[idx[breaks], idx[-1]] + 1
    pre, post = max(1, int(0.0003 * sr)), max(2, int(0.001 * sr))    # a click's tail rings on a little
    longest = int(max_len * sr)
    near = int(0.006 * sr)
    side = int(0.001 * sr)
    k_local = k * 0.6
    ah = np.abs(h)
    cand = [(s, e) for s, e in zip(starts, ends) if e - s <= longest]
    out = []
    for i, (s, e) in enumerate(cand):
        # many spikes close together are a percussive sound (snare, hi-hat, consonant), not crackle
        crowd = sum(1 for j in range(max(0, i - 4), min(len(cand), i + 5)) if j != i and abs(cand[j][0] - s) < 2 * near)
        if crowd >= 3:
            continue
        # a click stands far above the sound just before AND just after it; a drum hit is followed by
        # its own loud decay, so the "after" side fails
        peak = float(ah[s:e].max())
        before = ah[max(0, s - near):max(0, s - side)]
        after = ah[min(len(h), e + side):min(len(h), e + near)]
        if len(before) < side or len(after) < side:
            continue
        rb = float(np.sqrt(np.mean(before ** 2))) + 1e-9
        ra = float(np.sqrt(np.mean(after ** 2))) + 1e-9
        if peak < k_local * rb or peak < k_local * ra:
            continue
        out.append((max(0, s - pre), min(len(x), e + post)))
    return out


def _ar_fill(seg, miss, order):
    """Fill the samples of `seg` flagged in `miss` from an AR model fitted to the rest (least squares)."""
    known = ~miss
    n = len(seg)
    # fit: predict each sample from the `order` before it, using only fully known windows
    cs = np.r_[0, np.cumsum(miss)]
    t = np.arange(order, n)
    rows = t[(cs[t + 1] - cs[t - order]) == 0]            # windows with nothing missing
    if len(rows) < 2 * order:
        return None
    lag = np.stack([seg[rows - j] for j in range(1, order + 1)], axis=1)
    coef, *_ = np.linalg.lstsq(lag, seg[rows], rcond=None)
    # prediction-error filter e[t] = x[t] - sum a_j x[t-j]; choose the missing samples to minimise |e|^2
    m_idx = np.flatnonzero(miss)
    lo = max(order, m_idx[0])
    hi = min(n, m_idx[-1] + order + 1)
    T = np.arange(lo, hi)
    E = np.zeros((len(T), n), np.float64)
    E[np.arange(len(T)), T] = 1.0
    for j in range(1, order + 1):
        E[np.arange(len(T)), T - j] = -coef[j - 1]
    Em, Ek = E[:, m_idx], E[:, known]
    rhs = -(Ek @ seg[known].astype(np.float64))
    fill, *_ = np.linalg.lstsq(Em, rhs, rcond=None)
    out = seg.copy()
    out[m_idx] = fill
    return out


def declick(x, sr, strength="normal", progress=lambda f: None):
    """x: (channels, n). Returns (cleaned copy, number of clicks repaired)."""
    if strength not in STRENGTHS:
        strength = "normal"
    _, max_len, order = STRENGTHS[strength]
    longest_gap = int(2 * max_len * sr)          # never rebuild more than a few ms in one go
    y = np.array(x, dtype=np.float32, copy=True)
    ctx = max(4 * order, int(0.012 * sr))
    total = 0
    chans = y.shape[0]
    for c in range(chans):
        runs = find_clicks(y[c], sr, strength)
        # merge runs whose context windows would overlap, so each fill sees clean surroundings
        merged = []
        for s, e in runs:
            if merged and s - merged[-1][1] < order and e - merged[-1][0] <= longest_gap:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((s, e))
        mask = np.zeros(y.shape[1], bool)
        for s, e in merged:
            mask[s:e] = True
        for i, (s, e) in enumerate(merged):
            a, b = s - ctx, e + ctx
            if a < 0 or b > y.shape[1]:
                continue
            seg = y[c, a:b].astype(np.float64)
            miss = mask[a:b].copy()
            fill = _ar_fill(seg, miss, order)
            if fill is None or not np.all(np.isfinite(fill)):
                continue
            # A repair may never be louder than the music around it: an unstable fill would otherwise add a
            # huge spike (heard as a click, and it makes anything that levels by the peak turn everything down).
            ctx_peak = float(np.abs(seg[~miss]).max()) if (~miss).any() else 0.0
            if float(np.abs(fill[miss]).max()) > 1.2 * ctx_peak + 1e-6:
                continue
            y[c, a:b] = fill.astype(np.float32)
            total += 1
            if i % 200 == 0:
                progress((c + i / max(1, len(merged))) / chans)
    progress(1.0)
    return y, total          # only the repaired samples differ from the input
