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


def _attack_rise(x, pos, sr):
    """How much each hit jumps out of what came before it (dB): energy of the first 10 ms vs the 20 ms before."""
    a, b = int(0.02 * sr), int(0.01 * sr)
    out = np.zeros(len(pos), np.float32)
    for i, p in enumerate(pos):
        pre = x[max(0, p - a):p]
        post = x[p:p + b]
        if len(pre) < a // 2 or len(post) < b // 2:
            continue
        out[i] = 10 * np.log10((np.mean(post ** 2) + 1e-12) / (np.mean(pre ** 2) + 1e-12))
    return out


def _flatness(F):
    """Spectral flatness of each hit's band shape: ~1 for white noise, low for a drum or note with a body."""
    p = np.exp(F.astype(np.float64))
    return np.exp(np.log(p + 1e-12).mean(1)) / (p.mean(1) + 1e-12)


def on_grid(pos, sr, beat_times, tol=0.03):
    """Which hits land within `tol` seconds of a 16th note of the beat grid."""
    bt = np.asarray(beat_times, float)
    if len(bt) < 2:
        return np.ones(len(pos), bool)
    sub = np.concatenate([np.linspace(bt[i], bt[i + 1], 5)[:-1] for i in range(len(bt) - 1)] + [bt[-1:]])
    t = np.asarray(pos, float) / sr
    j = np.clip(np.searchsorted(sub, t), 1, len(sub) - 1)
    return np.minimum(np.abs(sub[j] - t), np.abs(sub[j - 1] - t)) <= tol


MAX_FLATNESS = 0.6      # a "sound" this flat is noise (hiss, separation wash), not a drum or a note
MIN_RISE_DB = 4.0       # a real hit jumps at least this much out of what came before it
MIN_ON_GRID = 0.6       # at least this share of a sound's hits must land on the 16th-note grid


def find_sounds(x, sr, min_count=6, similarity=0.9, beat_times=None):
    """Group a track's hits into repeated sounds. Returns (positions, list of clusters as index arrays,
    features, levels, band centres). Groups that are noise (flat spectrum, no real attack) or that mostly
    fall off the beat grid (when `beat_times` is given) are left out: they are separation leftovers, not
    sounds the song plays."""
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
    rise = _attack_rise(x, pos, sr)
    flat = _flatness(F)
    grid = on_grid(pos, sr, beat_times) if beat_times is not None else np.ones(len(pos), bool)
    keep = []
    for c in clusters:
        if np.median(flat[c]) > MAX_FLATNESS and np.median(rise[c]) < 2 * MIN_RISE_DB:
            continue                                   # noise bursts
        if np.median(rise[c]) < MIN_RISE_DB:
            continue                                   # no attack: a swell or wash, not a hit
        if grid[c].mean() < MIN_ON_GRID:
            continue                                   # mostly off the beat: not part of the groove
        keep.append(c)
    return pos, keep, F, lv, centers


def align(best, target, max_shift):
    """Sample offset that best lines `best` up with `target` (cross-correlation of the attacks)."""
    n = min(len(best), len(target)) - 2 * max_shift
    if n <= 32:
        return 0
    a = best[max_shift:max_shift + n]
    scores = [float(np.dot(a, target[max_shift + s:max_shift + s + n])) for s in range(-max_shift, max_shift + 1)]
    return int(np.argmax(scores)) - max_shift
