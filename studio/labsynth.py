"""Studio Lab voices: a bass and a drum machine that play what the song itself can't give back.

Instrumental Studio rebuilds damaged notes and hits from clean copies of the same sound elsewhere in the song. When
there is no clean copy (a bass note only ever played under the singing, a drum sound that never appears without the
voice over it), these voices play it instead, built the way Studio Lab builds its instruments - and then fitted to
the song: the bass to the harmonics and decay measured on the song's own clean bass notes, each drum voice to the
band shape (first 80 ms, the same measurement the hit finder uses) and the decay of the hits it replaces.
"""
import numpy as np
from scipy import signal

from . import hits as HT


# ------------------------------------------------------------------ bass
def bass_profile(clips, f0s, sr, n_harm=12):
    """What the song's bass sounds like: the level of each harmonic (relative to the loudest) and how fast a note
    decays, measured on clean notes. clips: mono note audio from its start; f0s: their pitches (Hz)."""
    amps, decays = [], []
    for y, f0 in zip(clips, f0s):
        a, b = int(0.02 * sr), int(0.14 * sr)
        if len(y) < b or f0 <= 0:
            continue
        seg = y[a:b] * np.hanning(b - a)
        S = np.abs(np.fft.rfft(seg, 16384))
        f = np.fft.rfftfreq(16384, 1 / sr)
        h = []
        for k in range(1, n_harm + 1):
            m = np.abs(f - k * f0) < max(0.03 * k * f0, 3)
            h.append(float(S[m].max()) if m.any() else 0.0)
        h = np.array(h)
        if h.max() <= 0:
            continue
        amps.append(h / h.max())
        e = np.sqrt(np.convolve(y ** 2, np.ones(512) / 512, "same"))
        t1, t2 = int(0.03 * sr), min(len(e) - 1, int(0.4 * sr))
        if t2 > t1 and e[t1] > 0 and e[t2] > 0:
            decays.append(float(np.log(e[t1] / e[t2]) / ((t2 - t1) / sr)))     # nepers per second
    if not amps:
        return None
    return {"amps": np.median(np.stack(amps), 0), "decay": float(np.clip(np.median(decays) if decays else 2.0, 0.2, 20))}


def bass_note(f0, dur, prof, sr, rng=None):
    """One bass note (mono) with the song's harmonics and decay: a sub-led additive voice like Studio Lab's
    'rollbass' (sine sub plus the upper partials that give the note its edge), with a short attack and release."""
    rng = rng or np.random.default_rng(0)
    n = int((dur + 0.03) * sr)
    t = np.arange(n) / sr
    y = np.zeros(n)
    for k, a in enumerate(prof["amps"], start=1):
        if a < 1e-3 or k * f0 > sr * 0.45:
            continue
        y += a * np.sin(2 * np.pi * k * f0 * t + rng.uniform(0, 2 * np.pi) * (k > 1))
    env = np.minimum(t / 0.004, 1) * np.exp(-prof["decay"] * t)
    rel = int(0.02 * sr)
    gate = int(dur * sr)
    if gate < n:
        env[gate:] *= np.exp(-np.arange(n - gate) / max(1, rel / 3))
    return (y * env).astype(np.float32)


# ------------------------------------------------------------------ drums
def _base_voice(kind, sr, n, rng):
    t = np.arange(n) / sr
    if "kick" in kind:
        f = 50 + 110 * np.exp(-t * 30)                       # 808/909-style pitch drop
        return np.sin(2 * np.pi * np.cumsum(f) / sr) * np.exp(-t * 8)
    if "snare" in kind or "clap" in kind or "rim" in kind:
        body = np.sin(2 * np.pi * 190 * t) * np.exp(-t * 25)
        noise = signal.sosfilt(signal.butter(2, [1500, 9000], "bandpass", fs=sr, output="sos"), rng.standard_normal(n))
        return 0.6 * body + noise * np.exp(-t * 18)
    if "tom" in kind:
        f = 90 + 60 * np.exp(-t * 15)
        return np.sin(2 * np.pi * np.cumsum(f) / sr) * np.exp(-t * 9)
    # hats, cymbals, shakers and anything else: bright noise
    noise = signal.sosfilt(signal.butter(2, 6000, "highpass", fs=sr, output="sos"), rng.standard_normal(n))
    return noise * np.exp(-t * (40 if "hat" in kind else 8))


def drum_voice(kind, shape, decay, sr, seed=0):
    """A Studio Lab drum-machine voice for `kind` (the hit finder's name: kick, snare, hi-hat ...), its spectrum
    fitted to `shape` (the hit finder's log band shape) and its length to `decay` (nepers per second)."""
    rng = np.random.default_rng(seed)
    n = int(0.6 * sr)
    y = _base_voice(kind, sr, n, rng)
    B, centers = HT._bands(sr)
    F, _ = HT._hit_features(y.astype(np.float32), np.array([0]), sr, B)
    gain_db = np.clip((np.asarray(shape) - F[0]) * 10 / np.log(10), -30, 30)       # per band, in dB
    # apply the band gains as a smooth EQ over the whole voice
    Y = np.fft.rfft(y, 2 * n)
    fr = np.fft.rfftfreq(2 * n, 1 / sr)
    g = np.interp(np.log(np.maximum(fr, 20)), np.log(centers), gain_db, left=gain_db[0], right=gain_db[-1])
    y = np.fft.irfft(Y * 10 ** (g / 20), 2 * n)[:n]
    t = np.arange(n) / sr
    y *= np.exp(-max(0.0, decay - 6.0) * t)                  # shorten to the hits' own decay
    y[-int(0.01 * sr):] *= np.linspace(1, 0, int(0.01 * sr))
    return y.astype(np.float32)


def decay_of(x, pos, sr):
    """How fast hits die away (nepers per second, median over the hits), from 10 ms to 150 ms after each."""
    out = []
    for p in pos:
        a, b = p + int(0.01 * sr), p + int(0.15 * sr)
        if b > len(x):
            continue
        e = np.sqrt(np.convolve(x[a:b] ** 2, np.ones(256) / 256, "valid"))
        if len(e) > 10 and e[0] > 0 and e[-1] > 0:
            out.append(np.log(e[0] / e[-1]) / ((b - a) / sr))
    return float(np.clip(np.median(out), 2, 80)) if out else 20.0
