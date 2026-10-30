"""Best hits: find the repeated one-shot sounds of a track (kick, snare, hi-hat, toms...), pick the
cleanest example of each, and rebuild the hits the vocals damaged from it.

A drum kit plays the same few sounds hundreds of times, so for any kick the vocals smeared there are
dozens of clean kicks to borrow from. Each damaged hit is replaced by the clean one, lined up to the
sample and matched in level using the frequencies the vocals left alone.
"""
import numpy as np
from scipy.ndimage import maximum_filter1d, uniform_filter1d

EPS = 1e-12


def _bands(sr, n=32, fmin=30.0, fmax=16000.0):
    freqs = np.fft.rfftfreq(2048, 1.0 / sr)
    edges = np.geomspace(fmin, min(fmax, sr / 2 * 0.95), n + 1)
    M = np.zeros((n, len(freqs)), np.float32)
    for b in range(n):
        M[b] = (freqs >= edges[b]) & (freqs < edges[b + 1])
        if not M[b].any():
            M[b, np.argmin(np.abs(freqs - edges[b]))] = 1
    return M / M.sum(1, keepdims=True), np.sqrt(edges[:-1] * edges[1:])


def onsets(x, sr, hop=256, min_gap=0.06):
    """Sample positions of sharp attacks (spectral flux peaks)."""
    n = (len(x) - 2048) // hop
    if n < 10:
        return np.zeros(0, int)
    # frames are built a block at a time: an index table for the whole song would need ~700 MB
    win = np.hanning(2048).astype(np.float32)
    cols = np.arange(2048)[None, :]
    out = []
    step = 1024
    for i in range(0, n, step):
        idx = np.arange(i, min(n, i + step))[:, None] * hop + cols
        S = np.abs(np.fft.rfft(x[idx] * win, axis=1))
        out.append(np.log1p(100 * S).astype(np.float32))
    L = np.vstack(out)
    flux = np.maximum(0, np.diff(L, axis=0)).sum(1)
    flux = np.r_[0, flux]
    base = uniform_filter1d(flux, int(0.5 * sr / hop)) + 1e-9
    peak = (flux == maximum_filter1d(flux, int(min_gap * sr / hop) | 1)) & (flux > 2.0 * base) \
        & (flux > np.percentile(flux, 60))
    return np.flatnonzero(peak) * hop + 1024 - hop // 2


def _hit_features(x, pos, sr, B):
    """Band-energy shape of the first 80 ms of each hit (log, level-normalised) and its level."""
    n = int(0.08 * sr)
    win = np.hanning(2 * n)[n:].astype(np.float32)          # decaying window: favour the attack
    feats, levels = [], []
    for p in pos:
        seg = x[p:p + n]
        if len(seg) < n:
            seg = np.pad(seg, (0, n - len(seg)))
        S = np.abs(np.fft.rfft(seg * win, 2048)) ** 2
        e = np.log(B @ S + 1e-10)
        levels.append(float(e.max()))
        feats.append(e - e.max())
    return np.array(feats, np.float32), np.array(levels, np.float32)


def name_sound(shape, centers):
    p = np.exp(shape)
    c = float((p * centers).sum() / (p.sum() + EPS))
    low = float(p[centers < 150].sum() / (p.sum() + EPS))
    if low > 0.5:
        return "kick"
    if c > 5000:
        return "hi-hat / cymbal"
    if c > 900:
        return "snare / clap"
    return "tom / low drum"


def find_sounds(x, sr, min_count=6, similarity=0.9):
    """Group a track's hits into repeated sounds. Returns (positions, list of clusters as index arrays,
    features, levels, band centres)."""
    B, centers = _bands(sr)
    pos = onsets(x, sr)
    if len(pos) < min_count:
        return pos, [], None, None, centers
    F, lv = _hit_features(x, pos, sr, B)
    Z = F - F.mean(1, keepdims=True)
    Z /= np.linalg.norm(Z, axis=1, keepdims=True) + EPS
    unassigned = set(range(len(pos)))
    clusters = []
    order = np.argsort(-lv)                 # seed with loud, clear hits
    for i in order:
        if i not in unassigned:
            continue
        sims = Z @ Z[i]
        members = [j for j in unassigned if sims[j] >= similarity]
        if len(members) >= min_count:
            clusters.append(np.array(sorted(members)))
            unassigned -= set(members)
    return pos, clusters, F, lv, centers


def align(best, target, max_shift):
    """Sample offset that best lines `best` up with `target` (cross-correlation of the attacks)."""
    n = min(len(best), len(target)) - 2 * max_shift
    if n <= 32:
        return 0
    a = best[max_shift:max_shift + n]
    scores = [float(np.dot(a, target[max_shift + s:max_shift + s + n])) for s in range(-max_shift, max_shift + 1)]
    return int(np.argmax(scores)) - max_shift
