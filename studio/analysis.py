"""Beat tracking, bar grid, song-section detection and per-stem beat features."""
import numpy as np
from scipy.ndimage import uniform_filter1d, gaussian_filter1d

from .audio import EPS, N_FFT, HOP


# ----------------------------------------------------------------------------- filterbanks
def band_matrix(sr, n_bands=40, fmin=30.0, fmax=16000.0):
    """Triangular log-spaced bands. Returns (matrix [bands, bins] rows sum to 1, centre freqs)."""
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    fmax = min(fmax, sr / 2 * 0.95)
    edges = np.geomspace(fmin, fmax, n_bands + 2)
    M = np.zeros((n_bands, len(freqs)), np.float32)
    for b in range(n_bands):
        lo, c, hi = edges[b:b + 3]
        M[b] = np.clip(np.minimum((freqs - lo) / (c - lo), (hi - freqs) / (hi - c)), 0, None)
        if M[b].sum() == 0:
            M[b, np.argmin(np.abs(freqs - c))] = 1.0
    M /= M.sum(1, keepdims=True)
    return M, edges[1:-1]


def chroma_matrix(sr, fmin=50.0, fmax=5000.0):
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    C = np.zeros((12, len(freqs)), np.float32)
    ok = (freqs >= fmin) & (freqs <= fmax)
    midi = 69 + 12 * np.log2(freqs[ok] / 440.0)
    for pc in range(12):
        d = (midi - pc + 6) % 12 - 6
        C[pc, ok] = np.exp(-0.5 * (d / 0.45) ** 2)
    return C


# ----------------------------------------------------------------------------- beats
def onset_envelope(band_energy):
    """Spectral flux on log band energies -> normalised onset strength per frame."""
    L = np.log(band_energy + 1e-8 * band_energy.max() + EPS)
    flux = np.maximum(0.0, np.diff(L, axis=1)).sum(0)
    flux = np.concatenate([[0.0], flux])
    flux = flux - uniform_filter1d(flux, 43)
    flux = np.maximum(flux, 0)
    return (flux / (flux.std() + EPS)).astype(np.float32)


def estimate_period(env, fps, bpm=None):
    if bpm:
        return fps * 60.0 / bpm
    e = env - env.mean()
    n = len(e)
    f = np.fft.rfft(e, 2 * n)
    ac = np.fft.irfft(np.abs(f) ** 2)[:n]
    lags = np.arange(int(fps * 60 / 200), min(int(fps * 60 / 50) + 1, n - 1))
    bpm_l = 60.0 * fps / lags
    w = np.exp(-0.5 * (np.log2(bpm_l / 120.0) / 0.9) ** 2)
    sc = ac[lags] * w
    i = int(np.argmax(sc))
    if 0 < i < len(sc) - 1:
        a, b, c = sc[i - 1], sc[i], sc[i + 1]
        den = a - 2 * b + c
        delta = 0.5 * (a - c) / den if den != 0 else 0.0
        return float(lags[i] + np.clip(delta, -0.5, 0.5))
    return float(lags[i])


def track_beats(env, period, tightness=100.0):
    """Ellis (2007) dynamic-programming beat tracker. Returns beat frame indices."""
    n = len(env)
    score = env.astype(np.float64).copy()
    back = -np.ones(n, dtype=np.int64)
    for t in range(n):
        lo, hi = int(round(t - 2 * period)), int(round(t - period / 2))
        if hi <= 0:
            continue
        prev = np.arange(max(lo, 0), hi)
        if len(prev) == 0:
            continue
        cand = score[prev] - tightness * np.log((t - prev) / period) ** 2
        i = int(np.argmax(cand))
        score[t] = env[t] + cand[i]
        back[t] = prev[i]
    tail = max(1, int(period))
    t = n - tail + int(np.argmax(score[n - tail:]))
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats = np.array(beats[::-1], dtype=np.float64)
    # extend so the grid covers the whole song
    p = np.median(np.diff(beats)) if len(beats) > 1 else period
    while beats[0] - p >= -p * 0.5 and beats[0] > 0:
        beats = np.concatenate([[max(0.0, beats[0] - p)], beats])
    while beats[-1] < n - 1:
        beats = np.concatenate([beats, [min(n - 1.0, beats[-1] + p)]])
    beats = np.unique(np.round(beats).astype(np.int64))
    return beats


def downbeat_phase(beats, low_env, chroma, bpb=4):
    """Pick which beat in each group of `bpb` is the downbeat: strongest low-end hits + chord changes."""
    best, best_s = 0, -1e9
    ch = chroma / (np.linalg.norm(chroma, axis=0, keepdims=True) + EPS)
    nov = np.zeros(len(beats))
    for j in range(1, len(beats) - 1):
        a = ch[:, beats[j - 1]:beats[j]].mean(1)
        b = ch[:, beats[j]:beats[j + 1]].mean(1)
        nov[j] = 1 - float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + EPS))
    lo = np.array([low_env[max(0, b - 2):b + 3].max() for b in beats])
    lo = lo / (lo.mean() + EPS)
    nov = nov / (nov.mean() + EPS)
    for p in range(bpb):
        s = (lo[p::bpb].mean() + nov[p::bpb].mean())
        if s > best_s:
            best, best_s = p, s
    return best


# ----------------------------------------------------------------------------- sections
SECTION_COLORS = ["#5B8DEF", "#E8875B", "#4FB286", "#C46BD6", "#D9B44A", "#E05C7A", "#5BC0D9", "#8E8E8E"]


def detect_sections(bar_feats, bar_vocal, bar_energy, min_bars=4):
    """Novelty-based boundaries on a bar grid, then cluster sections into repeating labels.

    bar_feats: (n_bars, d) normalised features. Returns list of dicts with bar ranges & names.
    """
    n = len(bar_feats)
    if n < 2 * min_bars:
        return [{"start_bar": 0, "end_bar": n, "label": "A", "name": "Song", "vocal": bool(bar_vocal.mean() > 0.1)}]
    X = bar_feats - bar_feats.mean(0, keepdims=True)          # remove what every bar shares
    F = X / (np.linalg.norm(X, axis=1, keepdims=True) + EPS)
    S = F @ F.T
    k = min_bars
    g = np.outer(np.r_[-np.ones(k), np.ones(k)], np.r_[-np.ones(k), np.ones(k)])
    g *= np.outer(np.hanning(2 * k + 2)[1:-1], np.hanning(2 * k + 2)[1:-1])
    Sp = np.pad(S, k, mode="edge")
    nov = np.array([(Sp[i:i + 2 * k, i:i + 2 * k] * g).sum() for i in range(n)])
    nov = np.maximum(nov, 0)
    # vocal on/off is a strong boundary cue
    vb = (bar_vocal > 0.08).astype(float)
    nov = nov / (np.abs(nov).max() + EPS) + 0.6 * np.abs(np.diff(np.r_[vb[0], vb]))
    bounds = [0]
    for i in np.argsort(-nov):
        if nov[i] < 0.35:
            break
        if all(abs(i - b) >= min_bars for b in bounds) and n - i >= min_bars:
            bounds.append(int(i))
    bounds = sorted(bounds) + [n]
    segs = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    # label sections: two sections match if each bar of one has a close twin in the other
    vocal = [bar_vocal[a:b].mean() > 0.08 for a, b in segs]
    def sec_sim(i, j):
        # best bar-to-bar alignment (sequence of chords must match, not just the set of chords)
        (a, b), (c, d) = segs[i], segs[j]
        best = -1.0
        for off in range(-2, 3):
            ts = [t for t in range(b - a) if a + t < n and 0 <= c + t + off < n and c + t + off < d + 2]
            if len(ts) >= 2:
                best = max(best, float(np.mean([S[a + t, c + t + off] for t in ts])))
        return best
    within = np.median(np.diag(S, 4)) if n > 4 else 0.8
    labels = []
    for i in range(len(segs)):
        hit = None
        for j in range(i):
            if vocal[i] == vocal[j] and sec_sim(i, j) > 0.85 * within:
                hit = labels[j]
                break
        labels.append(hit if hit is not None else (max(labels) + 1 if labels else 0))
    # merge neighbours that got the same label
    merged_segs, merged_labels, merged_vocal = [], [], []
    for sg, l, v in zip(segs, labels, vocal):
        if merged_labels and merged_labels[-1] == l:
            merged_segs[-1] = (merged_segs[-1][0], sg[1])
        else:
            merged_segs.append(sg); merged_labels.append(l); merged_vocal.append(v)
    segs, labels, vocal = merged_segs, merged_labels, merged_vocal
    # human names
    energy = [bar_energy[a:b].mean() for a, b in segs]
    vocal_labels = sorted({l for l, v in zip(labels, vocal) if v})
    names = {}
    if vocal_labels:
        rep = {l: sum(1 for x in labels if x == l) for l in vocal_labels}
        en = {l: np.mean([e for e, x in zip(energy, labels) if x == l]) for l in vocal_labels}
        chorus = max(vocal_labels, key=lambda l: (rep[l] > 1, en[l]))
        verse_c = [l for l in vocal_labels if l != chorus]
        names[chorus] = "Chorus"
        if verse_c:
            verse = max(verse_c, key=lambda l: rep[l])
            names[verse] = "Verse"
    out, counts = [], {}
    for i, ((a, b), l, v) in enumerate(zip(segs, labels, vocal)):
        if l in names:
            base = names[l]
        elif v:
            base = "Bridge"
        elif i == 0:
            base = "Intro"
        elif i == len(segs) - 1:
            base = "Outro"
        else:
            base = "Instrumental"
        counts[base] = counts.get(base, 0) + 1
        out.append({"start_bar": a, "end_bar": b, "label": chr(65 + l % 26), "base": base, "vocal": bool(v),
                    "color": SECTION_COLORS[l % len(SECTION_COLORS)]})
    totals = {}
    for s in out:
        totals[s["base"]] = totals.get(s["base"], 0) + 1
    seen = {}
    for s in out:
        seen[s["base"]] = seen.get(s["base"], 0) + 1
        s["name"] = s["base"] + (f" {seen[s['base']]}" if totals[s["base"]] > 1 else "")
    return out


# ----------------------------------------------------------------------------- per-stem beat features
def beat_features(band_e, chroma_f, vocal_band_presence, beats):
    """Summaries per beat interval: log band energy, reliability-weighted chroma, vocal presence per band."""
    nb = len(beats) - 1
    nbands = band_e.shape[0]
    F = np.zeros((nb, nbands), np.float32)
    C = np.zeros((nb, 12), np.float32)
    R = np.zeros((nb, nbands), np.float32)
    floor = 1e-7 * band_e.mean() + EPS
    for j in range(nb):
        a, b = beats[j], max(beats[j + 1], beats[j] + 1)
        F[j] = np.log(band_e[:, a:b].mean(1) + floor)
        c = chroma_f[:, a:b].sum(1)
        C[j] = c / (np.linalg.norm(c) + EPS)
        R[j] = vocal_band_presence[:, a:b].mean(1)
    return F, C, R


def smooth_curve(x, sigma=2):
    return gaussian_filter1d(np.asarray(x, np.float64), sigma)
