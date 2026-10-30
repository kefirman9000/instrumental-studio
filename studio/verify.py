"""Checks against the original recording.

The original song is the one thing we know is right: every instrument sound in the finished
instrumental must already be in it, at the same moment. So a fix can be checked two ways:

- Does it add sound the original doesn't have there? A borrowed bar where the guitar played a
  different part, a note from another chord, or the backing guitar standing in for the lead all put
  energy at frequencies where the original is quiet. ("unsupported" energy)
- Do its attacks line up with the original's? A fix lined up to a slightly wrong beat makes drum hits
  flam or land late; the attacks of what it adds then match the original best at an offset.

Also here: cheap per-band energies for whole songs (for the quality score and best-of-models).
"""
import numpy as np
from scipy.ndimage import maximum_filter, uniform_filter1d

from .audio import EPS, HOP, N_FFT, stft

HEADROOM = 1.26          # a track may be up to ~2 dB louder than the mix in one spot (phase, smearing)


def onset_env(x, sr):
    """Attack strength per millisecond of a mono signal."""
    step = max(1, sr // 1000)
    e = np.abs(np.diff(np.asarray(x, np.float32), prepend=np.float32(x[0]) if len(x) else 0))
    n = len(e) // step
    if n < 4:
        return np.zeros(max(n, 0), np.float32)
    e = e[:n * step].reshape(n, step).mean(1)
    e = uniform_filter1d(e, 3)
    return np.maximum(0, np.diff(e, prepend=e[:1])).astype(np.float32)


def best_offset(a, b, max_ms=25):
    """Shift s (ms) where a[t] best matches b[t + s], and how much better that is than no shift."""
    n = min(len(a), len(b))
    if n <= 2 * max_ms + 10:
        return 0, 1.0
    a, b = a[:n], b[:n]
    core = a[max_ms:n - max_ms]
    scores = np.array([float(np.dot(core, b[max_ms + s:n - max_ms + s])) for s in range(-max_ms, max_ms + 1)])
    s = int(np.argmax(scores)) - max_ms
    zero = scores[max_ms]
    return s, float(scores.max() / (zero + EPS)) if zero > 0 else (np.inf if scores.max() > 0 else 1.0)


def check_region(raw, patch, orig, sr):
    """raw, patch, orig: (2, n) arrays for the same stretch. Returns
    u:          share of the energy the fix adds that the original can't account for (0..1)
    offset_ms:  how far the fix's attacks sit from the original's (0 when they line up or can't be told)
    """
    r0, p, o = raw.mean(0), patch.mean(0), orig.mean(0)
    S0 = np.abs(stft(r0))
    S1 = np.abs(stft(r0 + p))
    So = maximum_filter(np.abs(stft(o)), size=(3, 3))     # tolerate a bin or a frame of smear
    cap = (HEADROOM * So) ** 2
    unsup = lambda S: float(np.clip(S ** 2 - cap, 0, None).sum())
    added = float((np.abs(stft(p)) ** 2).sum())
    base = float((S0 ** 2).sum()) + EPS
    u = 0.0
    if added > 0.003 * base:
        u = float(np.clip((unsup(S1) - unsup(S0)) / (added + EPS), 0, 1))
    offset = 0
    a = onset_env(p, sr)
    if len(a) and a.max() > 8 * (a.mean() + EPS) and a.sum() > 0.05 * (onset_env(r0, sr).sum() + EPS):
        s, ratio = best_offset(a, onset_env(o, sr))
        if abs(s) >= 8 and ratio >= 1.25:
            offset = s
    return {"u": u, "offset_ms": int(offset)}


def band_energy(x, bands, max_frames=2048):
    """Per-band power (bands, frames) of a long mono signal, in blocks so memory stays small.
    Frame t is centred on sample t*HOP, like stft()."""
    x = np.asarray(x, np.float32)
    pad = N_FFT // 2
    xp = np.pad(x, (pad, pad + HOP))
    nf = 1 + len(x) // HOP
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)
    out = np.zeros((bands.shape[0], nf), np.float32)
    for f0 in range(0, nf, max_frames):
        f1 = min(nf, f0 + max_frames)
        idx = (np.arange(f0, f1) * HOP)[:, None] + np.arange(N_FFT)[None, :]
        idx = np.minimum(idx, len(xp) - 1)
        S = np.abs(np.fft.rfft(xp[idx] * win, axis=1)) ** 2
        out[:, f0:f1] = (bands @ S.T).astype(np.float32)
    return out


def clean_terms(Ek, Eo, Ev, vocal_frames, vocal_bands):
    """For one track over one stretch (band energies): how much it has that the original doesn't
    (separation artifacts, wrong parts), and how much it rises and falls with the singer (leftover
    vocal). Both 0..1, lower is cleaner."""
    tot = float(Ek.sum()) + EPS
    unsup = float(np.clip(Ek - (HEADROOM ** 2) * Eo, 0, None).sum() / tot)
    bleed = 0.0
    if vocal_frames.sum() >= 8:
        a = np.log(Ek[vocal_bands][:, vocal_frames] + 1e-9).ravel()
        b = np.log(Ev[vocal_bands][:, vocal_frames] + 1e-9).ravel()
        if a.std() > 1e-3 and b.std() > 1e-3:
            bleed = float(max(0.0, np.corrcoef(a, b)[0, 1]))
    return unsup, bleed


def clean_score(unsup, bleed):
    """0..100, higher is cleaner."""
    return int(round(100 * (1 - float(np.clip(0.8 * unsup + 0.7 * max(0.0, bleed - 0.25), 0, 1)))))
