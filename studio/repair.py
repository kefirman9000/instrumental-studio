"""Damage detection and same-instrument repair.

Idea: a separator damages an instrument where the vocals were louder than it — those time/frequency
cells were handed to the vocal stem. Songs repeat, so the same instrument usually plays the same part
somewhere the vocals were absent (intro, break, a vocal rest, another verse where the melody sat
elsewhere). For each vocal window we find the best-matching windows of THE SAME STEM, judged only on
the frequency bands the vocals did not cover, then compare: wherever vocals dominated and the stem
now has less energy than its donors say it should, that's damage, and we fill it from the donors.
"""
import numpy as np
from scipy.ndimage import gaussian_filter

from .audio import EPS, N_FFT, HOP, stft, istft, segment

XFADE = 1024          # samples of cross-fade at each window edge (half on each side of the boundary)
MARGIN = N_FFT        # extra context so STFT edges never reach the audible part


# ----------------------------------------------------------------------------- matching
def match_window(F, C, R, s, L, n_donors=3, exclude=None):
    """Rank donor windows (start beats) for target window [s, s+L) of one stem.

    F, C, R: per-beat log band energy, chroma, vocal presence per band (beats x bands).
    Returns (donors list of dicts, confidence 0..1).
    """
    nb = F.shape[0]
    starts = np.arange(0, nb - L + 1)
    valid = np.abs(starts - s) >= L
    if exclude is not None:
        valid &= ~exclude[starts]
    if valid.sum() < 1:
        return [], 0.0
    idx = starts[:, None] + np.arange(L)[None, :]
    Ft, Ct, Rt = F[s:s + L], C[s:s + L], R[s:s + L]
    Fd, Cd, Rd = F[idx], C[idx], R[idx]
    w = (1 - Rt)[None] * (1 - Rd)                       # judge only where neither had vocals
    # level-invariant timbre: remove each window's mean log level (gain is matched later)
    dt = (Ft - Ft.mean()); dd = (Fd - Fd.mean(axis=(1, 2), keepdims=True))
    bd = (w * (dd - dt[None]) ** 2).sum((1, 2)) / (w.sum((1, 2)) + EPS)
    lvl = np.abs(Fd.mean(axis=(1, 2)) - Ft.mean())      # mild preference for similar loudness
    cd = 1 - (Cd * Ct[None]).sum(2).mean(1)
    pen = (Rt[None] * Rd).sum((1, 2)) / (Rt.sum() + EPS)  # donor also had vocals where target did
    med_b = np.median(bd[valid]) + EPS
    med_c = np.median(cd[valid]) + EPS
    dist = 0.45 * bd / med_b + 0.45 * cd / med_c + 0.1 * lvl / (np.median(lvl[valid]) + EPS)
    score = np.where(valid, dist + 1.5 * pen, np.inf)
    chosen = []
    for d in np.argsort(score):
        if not np.isfinite(score[d]) or dist[d] >= 1.0:
            break
        if all(abs(int(d) - c["beat"]) >= L for c in chosen):
            chosen.append({"beat": int(d), "dist": float(dist[d]), "overlap": float(pen[d])})
        if len(chosen) >= n_donors:
            break
    if not chosen:
        return [], 0.0
    b = chosen[0]
    conf = float(np.clip(1.3 * (1 - b["dist"]), 0, 1) * np.clip(1 - b["overlap"], 0, 1))
    return chosen, conf


def refine_lag(env, t0, t1, d0, max_shift=4):
    """Sub-frame alignment of a donor using an instrument onset envelope. Returns lag in samples."""
    t = env[t0:t1]
    if len(t) < 8:
        return (d0 - t0) * HOP
    t = t - t.mean()
    best_k, best_c, cs = 0, -np.inf, {}
    for k in range(-max_shift, max_shift + 1):
        a = d0 + k
        if a < 0 or a + len(t) > len(env):
            continue
        d = env[a:a + len(t)] - env[a:a + len(t)].mean()
        c = float(t @ d / (np.linalg.norm(t) * np.linalg.norm(d) + EPS))
        cs[k] = c
        if c > best_c:
            best_k, best_c = k, c
    frac = 0.0
    if best_k - 1 in cs and best_k + 1 in cs:
        a, b, c = cs[best_k - 1], cs[best_k], cs[best_k + 1]
        den = a - 2 * b + c
        frac = float(np.clip(0.5 * (a - c) / den, -0.5, 0.5)) if den != 0 else 0.0
    return int(round((d0 - t0 + best_k + frac) * HOP))


def _window_weight(n, core_lo, core_hi):
    """Trapezoid with raised-cosine ramps centred on the core boundaries (sums to 1 across neighbours)."""
    w = np.zeros(n, np.float32)
    h = XFADE // 2
    a0, a1 = core_lo - h, core_lo + h
    b0, b1 = core_hi - h, core_hi + h
    x = np.arange(n)
    w[(x >= a1) & (x < b0)] = 1.0
    ra = (x >= a0) & (x < a1)
    w[ra] = np.sin(0.5 * np.pi * (x[ra] - a0) / (a1 - a0)) ** 2
    rb = (x >= b0) & (x < b1)
    w[rb] = np.cos(0.5 * np.pi * (x[rb] - b0) / (b1 - b0)) ** 2
    return w


# ----------------------------------------------------------------------------- repair
def repair_window(stem, vocals, inst_sum, s0, s1, lags, conf, strength, bands, band_centers, sr,
                  level_floor, mode="fill"):
    """Repair one window [s0, s1) samples of one stem from donor windows offset by `lags` samples.

    Returns dict(lo, patch[2, n], damage_map[bins? no: frames x bins mono], severity, ...).
    """
    lo, hi = s0 - XFADE // 2 - MARGIN, s1 + XFADE // 2 + MARGIN
    n = hi - lo
    ch = stem.shape[0]
    Xt = [stft(segment(stem[c], lo, hi)) for c in range(ch)]
    Vm = np.abs(stft(segment(vocals, lo, hi).mean(0)))
    Im = np.abs(stft(segment(inst_sum, lo, hi).mean(0)))
    r = Vm ** 2 / (Vm ** 2 + Im ** 2 + EPS)
    r_s = gaussian_filter(r, (2, 1))

    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    logf = np.log(np.maximum(freqs, 1.0))
    logc = np.log(band_centers)
    Xm = np.mean(Xt, axis=0)
    Am2 = np.abs(Xm) ** 2

    donors = []
    for lag in lags:
        Xd = [stft(segment(stem[c], lo + lag, hi + lag)) for c in range(ch)]
        Vd = np.abs(stft(segment(vocals, lo + lag, hi + lag).mean(0)))
        Id = np.abs(stft(segment(inst_sum, lo + lag, hi + lag).mean(0)))
        rd = gaussian_filter(Vd ** 2 / (Vd ** 2 + Id ** 2 + EPS), (2, 1))
        Dm2 = np.abs(np.mean(Xd, axis=0)) ** 2
        w = (1 - r_s) * (1 - rd)
        g_glob = np.sqrt((w * Am2).sum() / ((w * Dm2).sum() + EPS))
        g_glob = float(np.clip(g_glob, 0.25, 4.0))
        num = bands @ (w * Am2).sum(1)
        den = bands @ (w * Dm2).sum(1)
        rel = (bands @ w.sum(1)) / max(w.shape[1], 1)
        gb = np.sqrt(num / (den + EPS))
        lam = np.clip(rel / 0.4, 0, 1)
        lg = lam * np.log(np.clip(gb, 0.25, 4.0) + EPS) + (1 - lam) * np.log(g_glob)
        lg = gaussian_filter(lg, 1.5, mode="nearest")
        g_bin = np.exp(np.interp(logf, logc, lg)).astype(np.float32)[:, None]
        donors.append([X * g_bin for X in Xd])

    out = np.zeros((ch, n), np.float32)
    D_all = np.zeros_like(r, dtype=np.float32)
    E_tot, miss_tot = 0.0, 0.0
    for c in range(ch):
        mags = np.stack([np.abs(d[c]) for d in donors])
        E = np.median(mags, axis=0) if len(donors) > 2 else mags.mean(0)
        It = np.abs(Xt[c])
        Is = gaussian_filter(It, (1, 1))
        Es = gaussian_filter(E, (1, 1))
        deficit = np.clip((Es - Is) / (Es + EPS), 0, 1)
        if mode == "replace":   # the person pointed at this part: rebuild it from its matches, vocals or not
            D = np.full_like(deficit, strength * conf)
        else:
            D = strength * conf * np.sqrt(np.clip((r_s - 0.03) / 0.97, 0, 1)) * deficit
        D *= (Es > level_floor)
        D = np.clip(gaussian_filter(D, (1, 1)), 0, 1).astype(np.float32)
        T = (1 - D) * It + D * E
        ph = np.angle((1 - D) * Xt[c] + D * donors[0][c])
        Y = T * np.exp(1j * ph)
        out[c] = istft(Y, n)
        D_all += D / ch
        E_tot += float((Es ** 2).sum())
        miss_tot += float((np.clip(Es - Is, 0, None) ** 2 * np.sqrt(r_s)).sum())

    orig = segment(stem, lo, hi)
    w = _window_weight(n, s0 - lo, s1 - lo)
    patch = (out - orig) * w[None, :]
    added = float((patch ** 2).sum())
    total = float((orig ** 2).sum()) + EPS
    return {
        "lo": lo, "patch": patch.astype(np.float32), "D": D_all,
        "severity": float(np.clip(miss_tot / (E_tot + EPS), 0, 1)),
        "added_db": float(10 * np.log10(added / total + 1e-12)),
        "damage_area": float((D_all[:, (MARGIN // HOP):-(MARGIN // HOP)] > 0.15).mean()),
    }


# ----------------------------------------------------------------------------- vocal-bleed cleanup
def bleed_window(stem, vocals, s0, s1, strength=1.0):
    """Detect leftover vocal in an instrument stem: stem spectrum that moves with the vocal's spectrum.

    Returns None if no bleed is evident, else dict(lo, patch, score).
    """
    lo, hi = s0 - XFADE // 2 - MARGIN, s1 + XFADE // 2 + MARGIN
    n = hi - lo
    ch = stem.shape[0]
    Xs = [stft(segment(stem[c], lo, hi)) for c in range(ch)]
    S = np.abs(np.mean(Xs, axis=0))
    V = np.abs(stft(segment(vocals, lo, hi).mean(0)))
    sel = slice(int(250 * N_FFT / 44100), int(5000 * N_FFT / 44100))
    s, v = S[sel], V[sel]
    m = v > np.percentile(v, 85)
    if m.sum() < 50 or v.max() < EPS:
        return None
    ls, lv = np.log(s[m] + EPS), np.log(v[m] + EPS)
    rho = float(np.corrcoef(ls, lv)[0, 1])
    # compare with frames shifted in time: real bleed correlates only when aligned
    v_sh = np.roll(v, 7, axis=1)
    m2 = v_sh > np.percentile(v_sh, 85)
    rho_sh = float(np.corrcoef(np.log(s[m2] + EPS), np.log(v_sh[m2] + EPS))[0, 1])
    excess = rho - max(rho_sh, 0)
    if rho < 0.45 or excess < 0.2:
        return None
    alpha = float(np.median(s[m] / (v[m] + EPS)))
    alpha = min(alpha, 1.0)
    out = np.zeros((ch, n), np.float32)
    for c in range(ch):
        Sc = np.abs(Xs[c])
        est = alpha * V
        mask = np.clip(1 - strength * (est / (Sc + EPS)) ** 2, 0.1, 1)
        mask = gaussian_filter(np.sqrt(mask), (1, 1))
        out[c] = istft(Xs[c] * mask, n)
    orig = segment(stem, lo, hi)
    w = _window_weight(n, s0 - lo, s1 - lo)
    patch = (out - orig) * w[None, :]
    return {"lo": lo, "patch": patch, "score": float(np.clip(excess * 2, 0, 1)),
            "removed_db": float(10 * np.log10((patch ** 2).sum() / ((orig ** 2).sum() + EPS) + 1e-12))}


# ----------------------------------------------------------------------------- "watery" separation sound
def _watery(S, w):
    """How much a spectrogram twinkles: average jump of each cell away from its neighbourhood
    median (log scale), weighted by w. Separation's 'musical noise' scores high; real notes don't."""
    from scipy.ndimage import median_filter
    L = np.log(S + 1e-6)
    dev = np.abs(L - median_filter(L, size=(3, 5)))
    return float((dev * w).sum() / (w.sum() + EPS))


def artifact_score(stem, vocals, inst_sum, s0, s1, sr):
    lo, hi = s0, s1
    S = np.abs(stft(segment(stem, lo, hi).mean(0)))
    V = np.abs(stft(segment(vocals, lo, hi).mean(0)))
    I = np.abs(stft(segment(inst_sum, lo, hi).mean(0)))
    r = V ** 2 / (V ** 2 + I ** 2 + EPS)
    f = np.fft.rfftfreq(N_FFT, 1.0 / sr)[:, None]
    band = (f > 300) & (f < 8000)
    energy = S ** 2 / (S ** 2).max(initial=EPS)
    w_cov = band * (r > 0.3) * np.sqrt(energy)
    w_all = band * np.sqrt(energy)
    return _watery(S, w_cov) if w_cov.sum() > 1 else 0.0, _watery(S, w_all), float((band * (r > 0.3)).mean())


def smooth_window(stem, vocals, inst_sum, s0, s1, strength=1.0):
    """Calm the twinkling under the vocals: blend each covered cell toward its neighbourhood median."""
    from scipy.ndimage import median_filter
    lo, hi = s0 - XFADE // 2 - MARGIN, s1 + XFADE // 2 + MARGIN
    n = hi - lo
    V = np.abs(stft(segment(vocals, lo, hi).mean(0)))
    I = np.abs(stft(segment(inst_sum, lo, hi).mean(0)))
    r = gaussian_filter(V ** 2 / (V ** 2 + I ** 2 + EPS), (2, 1))
    alpha = np.clip(strength * np.sqrt(np.clip((r - 0.1) / 0.9, 0, 1)), 0, 1) * 0.85
    out = np.zeros((stem.shape[0], n), np.float32)
    for c in range(stem.shape[0]):
        X = stft(segment(stem[c], lo, hi))
        M = np.abs(X)
        Mm = median_filter(M, size=(3, 5))
        out[c] = istft((M * (1 - alpha) + Mm * alpha) * np.exp(1j * np.angle(X)), n)
    orig = segment(stem, lo, hi)
    w = _window_weight(n, s0 - lo, s1 - lo)
    return {"lo": lo, "patch": ((out - orig) * w[None, :]).astype(np.float32)}


# ----------------------------------------------------------------------------- leftover "s" and "t" sounds
def ess_window(stem, vocals, s0, s1, sr, strength=1.0):
    """Separation often leaves a singer's "s", "sh" and "t" in the instrument tracks: short bursts of high
    frequencies that line up with the vocal's own. Turn the instrument down in that range, only during
    those bursts, by about how much of it moves with the vocal. Returns None if there's nothing to do."""
    lo, hi = s0 - XFADE // 2 - MARGIN, s1 + XFADE // 2 + MARGIN
    n = hi - lo
    f = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    band = (f >= 4500) & (f <= 12000)
    V = np.abs(stft(segment(vocals, lo, hi).mean(0)))
    Xs = [stft(segment(stem[c], lo, hi)) for c in range(stem.shape[0])]
    S = np.abs(np.mean(Xs, axis=0))
    ve = (V[band] ** 2).sum(0)
    if ve.max() <= EPS:
        return None
    vb = 10 * np.log10(ve + EPS)
    floor = np.percentile(vb, 50)
    burst = vb > max(floor + 12, vb.max() - 30)            # the vocal's sibilant moments
    # bursts are short; long stretches of high-frequency vocal are breathy singing, leave those alone
    from scipy.ndimage import label as _label
    lab, nlab = _label(burst)
    for i in range(1, nlab + 1):
        if (lab == i).sum() * HOP / sr > 0.25:
            burst[lab == i] = False
    if burst.sum() < 2:
        return None
    sb, vbnd = S[band][:, burst], V[band][:, burst]
    ls, lv = np.log(sb + EPS).ravel(), np.log(vbnd + EPS).ravel()
    if len(ls) < 20 or not (np.corrcoef(ls, lv)[0, 1] >= 0.35):
        return None                                       # the instrument's highs don't follow the vocal's
    alpha = float(min(np.median(sb / (vbnd + EPS)), 1.0))
    G = np.ones_like(S)
    est = alpha * V
    gb = np.clip(1 - strength * (est / (S + EPS)) ** 2, 0.15, 1.0)
    mask = np.zeros_like(S, dtype=bool)
    mask[np.ix_(band, burst)] = True
    G[mask] = gb[mask]
    G = gaussian_filter(G, (1, 1))
    out = np.zeros((stem.shape[0], n), np.float32)
    for c in range(stem.shape[0]):
        out[c] = istft(Xs[c] * G, n)
    orig = segment(stem, lo, hi)
    w = _window_weight(n, s0 - lo, s1 - lo)
    patch = ((out - orig) * w[None, :]).astype(np.float32)
    removed = float(10 * np.log10((patch ** 2).sum() / ((orig ** 2).sum() + EPS) + 1e-12))
    if removed < -45:
        return None
    return {"lo": lo, "patch": patch, "bursts": int(nlab), "removed_db": removed}
