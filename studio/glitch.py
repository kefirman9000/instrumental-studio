"""Find and fix things that sound off: clicks/pops and sudden dropouts in an instrument.

Every fix is returned as a patch (fixed - current) so it goes through the same review flow as
repairs: nothing changes until the person applies it, and before/after can be compared.
"""
import numpy as np
from scipy.interpolate import CubicSpline
from scipy.ndimage import uniform_filter1d, maximum_filter1d

EPS = 1e-12


def _block_rms(x, n):
    m = len(x) // n
    if not m:
        return np.full(len(x), np.sqrt((x ** 2).mean() + EPS))
    r = np.repeat(np.sqrt((x[:m * n].reshape(m, n) ** 2).mean(1) + EPS), n)
    return np.pad(r, (0, len(x) - len(r)), mode="edge")


def _level_db(x, a, b):
    seg = x[max(0, a):max(a + 1, b)]
    return 10 * np.log10((seg ** 2).mean() + EPS)


def _level_db(x, a, b):
    seg = x[max(0, a):max(a + 1, b)]
    return 10 * np.log10((seg ** 2).mean() + EPS)


def find_clicks(x, sr, sensitivity=1.0):
    """Clicks/pops: isolated spikes far above the local level, in sound that carries on at the same
    level either side (so note starts and stops, which change the level, are not counted).
    Returns list of (start, end) sample ranges."""
    if len(x) < sr // 10:
        return []
    d2 = np.zeros_like(x)
    d2[1:-1] = x[2:] - 2 * x[1:-1] + x[:-2]
    a = np.abs(d2)
    local = uniform_filter1d(_block_rms(d2, 256), int(0.02 * sr))
    wide = uniform_filter1d(_block_rms(d2, 1024), int(0.2 * sr))
    level = np.maximum(np.maximum(local, wide * 0.7), 1e-4)
    hits = np.flatnonzero(a > (14.0 / sensitivity) * level)
    if not len(hits):
        return []
    events, start, last = [], hits[0], hits[0]
    gap = int(0.004 * sr)
    for h in hits[1:]:
        if h - last > gap:
            events.append((start, last))
            start = h
        last = h
    events.append((start, last))
    out, w = [], int(0.01 * sr)
    for s, e in events:
        if e - s > int(0.003 * sr):
            continue
        before, after = _level_db(x, s - w, s - 16), _level_db(x, e + 16, e + w)
        if abs(before - after) > 4.0 or max(before, after) < -70:
            continue                                   # a note starting or stopping, or silence
        out.append((max(0, s - 8), min(len(x), e + 9)))
    return out


def fix_click(y, s, e):
    """Replace samples [s, e) of every channel with a smooth curve through the neighbours."""
    out = y.copy()
    ctx = 48
    a, b = max(0, s - ctx), min(y.shape[1], e + ctx)
    xs = np.r_[np.arange(a, s), np.arange(e, b)]
    if len(xs) < 8:
        return out
    for c in range(y.shape[0]):
        cs = CubicSpline(xs, y[c, xs])
        out[c, s:e] = cs(np.arange(s, e))
    return out


def find_dropouts(x, sr, suspect=None, min_db=24.0):
    """Sudden gaps: the level falls by min_db within ~10 ms, stays down 10-80 ms, and comes back to
    about the same level, while the instrument is otherwise playing. `suspect(i)` (sample index ->
    bool) limits results to places where separation or an edit could have caused it, so musical
    rests are not reported. Returns list of (start, end) sample ranges."""
    hop = int(0.005 * sr)
    m = len(x) // hop
    if m < 100:
        return []
    rms = np.sqrt((x[:m * hop].reshape(m, hop) ** 2).mean(1) + EPS)
    db = 20 * np.log10(rms + EPS)
    ref = 20 * np.log10(np.percentile(rms, 95) + EPS)
    around = maximum_filter1d(db, 41)
    playing = around > ref - 30
    low = (db < around - min_db) & playing
    out = []
    i = 0
    while i < m:
        if not low[i]:
            i += 1
            continue
        j = i
        while j < m and low[j]:
            j += 1
        dur_ms = (j - i) * 5
        if 10 <= dur_ms <= 80 and i >= 4 and j + 4 < m:
            before, after = db[i - 4:i - 1].mean(), db[j + 1:j + 4].mean()
            sharp = before - db[i] >= min_db * 0.8 and after - db[j - 1] >= min_db * 0.8
            if sharp and abs(before - after) <= 6 and (suspect is None or suspect(i * hop)):
                out.append((i * hop, j * hop))
        i = j
    return out


def fix_dropout(y, s, e, sr):
    """Fill a short gap with the audio just before it (like packet-loss concealment), cross-faded."""
    out = y.copy()
    n = e - s
    fade = max(8, min(int(0.005 * sr), n // 2))
    src_a = max(0, s - n)
    seg = y[:, src_a:src_a + n]
    if seg.shape[1] < n:
        return out
    w = np.ones(n, np.float32)
    w[:fade] = np.linspace(0, 1, fade)
    w[-fade:] = np.linspace(1, 0, fade)
    out[:, s:e] = y[:, s:e] * (1 - w) + seg * w
    return out


def _main_band(x, sr):
    """Frequency range holding the middle 80% of an instrument's energy."""
    from scipy.signal import welch
    f, pxx = welch(x[: sr * 60], sr, nperseg=4096)
    c = np.cumsum(pxx) / (pxx.sum() + EPS)
    lo, hi = f[np.searchsorted(c, 0.1)], f[np.searchsorted(c, 0.9)]
    return max(30.0, lo), min(sr / 2 * 0.9, max(hi, lo * 2))


def _also_dips_in(orig, sr, band, s, e):
    """Does the original recording also go quiet in this instrument's range here? Then it's a rest."""
    from scipy import signal as sig
    pad = int(0.05 * sr)
    a, b = max(0, s - pad), min(orig.shape[-1], e + pad)
    sos = sig.butter(2, band, "bandpass", fs=sr, output="sos")
    y = sig.sosfiltfilt(sos, orig[a:b])
    before = _level_db(y, 0, s - a)
    inside = _level_db(y, s - a, e - a)
    return before - inside > 10


def scan(y, sr, stem_name, raw=None, vocal_at=None, original=None, beats=None):
    """All glitches in one rendered stem.

    raw: the stem straight from separation; vocal_at(i): how much singing at sample i (0..1);
    original: the original recording (mono), used to tell real rests from dropouts;
    beats: beat positions in samples (a gap that ends right on a beat is a player's articulation).
    Returns list of dicts with kind, start, end, lo, patch.
    """
    x = y.mean(0)
    band = _main_band(x, sr) if original is not None else None
    found = []
    if stem_name != "drums":           # drum hits are clicks by nature
        for s, e in find_clicks(x, sr):
            lo, hi = max(0, s - 64), min(y.shape[1], e + 64)
            fixed = fix_click(y[:, lo:hi], s - lo, e - lo)
            found.append({"kind": "click", "start": s, "end": e, "lo": lo,
                          "patch": (fixed - y[:, lo:hi]).astype(np.float32)})

    def suspect(i):
        edited = raw is not None and np.abs(y[:, i:i + 2048] - raw[:, i:i + 2048]).max() > 1e-4
        sung = vocal_at is not None and vocal_at(i) > 0.3
        return edited or sung
    for s, e in (find_dropouts(x, sr, suspect) if stem_name != "drums" else []):
        if band is not None and _also_dips_in(original, sr, band, s, e):
            continue
        if beats is not None and len(beats):
            k = np.searchsorted(beats, e)
            near = min(abs(int(beats[min(k, len(beats) - 1)]) - e), abs(int(beats[max(k - 1, 0)]) - e))
            if near < 0.035 * sr:
                continue
        lo, hi = max(0, s - (e - s) - 512), min(y.shape[1], e + 512)
        fixed = fix_dropout(y[:, lo:hi], s - lo, e - lo, sr)
        found.append({"kind": "dropout", "start": s, "end": e, "lo": lo,
                      "patch": (fixed - y[:, lo:hi]).astype(np.float32)})
    return found
