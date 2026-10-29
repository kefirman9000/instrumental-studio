"""Built-in demo: synthesises a short song with repeating sections, then damages its stems the way a
vocal separator does (instrument energy under loud vocals is handed to the vocal stem, plus a little
vocal bleed left in one instrument). Lets you try the whole app without Demucs, and gives a known
ground truth for testing the repair."""
import numpy as np
from scipy import signal

from .audio import stft, istft, EPS

SR = 44100


def _midi(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def _env(n, a, r, sr=SR):
    t = np.arange(n) / sr
    return np.minimum(1, t / max(a, 1e-4)) * np.exp(-t / r)


def synth_song(seed=7, bpm=112):
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    bar = 4 * beat
    A = [(57, "m"), (53, ""), (48, ""), (55, "")]      # Am F C G
    Bp = [(50, "m"), (55, ""), (52, "m"), (57, "m")]   # Dm G Em Am
    plan = [("Intro", A, 4, False), ("Verse", A, 8, True), ("Chorus", Bp, 8, True), ("Break", Bp, 4, False),
            ("Verse", A, 8, True), ("Chorus", Bp, 8, True), ("Outro", A, 4, False)]
    n_bars = sum(p[2] for p in plan)
    N = int(n_bars * bar * SR) + SR
    drums, bass, piano, guitar, vox = (np.zeros((2, N), np.float32) for _ in range(5))
    hp = signal.butter(4, 7000, "hp", fs=SR, output="sos")
    bp = signal.butter(2, [900, 7000], "bp", fs=SR, output="sos")

    def add(buf, x, t0, pan=0.0, gain=1.0):
        i = int(t0 * SR)
        x = x[: max(0, N - i)]
        buf[0, i:i + len(x)] += x * gain * np.sqrt(0.5 * (1 - pan))
        buf[1, i:i + len(x)] += x * gain * np.sqrt(0.5 * (1 + pan))

    def tones(root, q):
        return [root, root + (3 if q == "m" else 4), root + 7]

    t_bar = 0.0
    for name, prog, nb, has_vox in plan:
        for bi in range(nb):
            root, q = prog[bi % 4]
            ch = tones(root, q)
            t0 = t_bar
            # drums
            for b in range(4):
                tb = t0 + b * beat
                n = int(0.35 * SR); t = np.arange(n) / SR
                if b in (0, 2):
                    f = 48 + 75 * np.exp(-t * 28)
                    add(drums, np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 9) * rng.uniform(.85, 1), tb, 0, .9)
                else:
                    sn = signal.sosfilt(bp, rng.standard_normal(n)) * np.exp(-t * 16) * 1.6
                    sn += np.sin(2 * np.pi * 185 * t) * np.exp(-t * 22) * .5
                    add(drums, sn * rng.uniform(.8, 1), tb, .05, .55)
                subdiv = 4 if prog is Bp else 2          # busier hats in the chorus
                for h in range(subdiv):
                    m = int(0.06 * SR)
                    hh = signal.sosfilt(hp, rng.standard_normal(m)) * np.exp(-np.arange(m) / SR * 70)
                    add(drums, hh * rng.uniform(.25, .5), tb + h * beat / subdiv, .4, .7)
            # bass: root per beat, occasional octave
            for b in range(4):
                m = root - 24 + (12 if (b == 3 and rng.random() < .4) else 0)
                n = int(beat * .95 * SR); t = np.arange(n) / SR
                f = _midi(m)
                x = sum(np.sin(2 * np.pi * f * k * t) / k ** 1.3 for k in range(1, 7))
                add(bass, x * _env(n, .005, .6) * rng.uniform(.8, 1), t0 + b * beat, 0, .32)
            # piano-ish pad: chord sustained over the bar
            n = int(bar * SR); t = np.arange(n) / SR
            for m in ch:
                f = _midi(m + 12 if m < 52 else m)
                x = sum(np.sin(2 * np.pi * f * k * t + rng.uniform(0, 6)) * np.exp(-t * (0.6 + .5 * k)) / k ** 1.6
                        for k in range(1, 9))
                add(piano, x * _env(n, .01, 3.0), t0, -.3, .09)
            # guitar-ish plucked arpeggio in eighths, pattern varies per bar
            pats = [[0, 1, 2, 3, 2, 1, 0, 1], [0, 2, 1, 3, 0, 2, 1, 3], [0, 1, 2, 1, 3, 2, 1, 2]]
            pat = pats[rng.integers(len(pats))]
            for e, idx in enumerate(pat):
                m = (ch + [ch[0] + 12])[idx] + 12
                n = int(beat * SR); t = np.arange(n) / SR
                f = _midi(m)
                x = sum(np.sin(2 * np.pi * f * k * t) * np.exp(-t * (3 + 2.2 * k)) / k for k in range(1, 12))
                add(guitar, x * _env(n, .002, .5) * rng.uniform(.6, 1), t0 + e * beat / 2, .35, .16)
            # vocal melody (formant-filtered harmonic tone) on most beats
            if has_vox:
                b = 0
                while b < 4:
                    dur = 2 if rng.random() < .3 and b < 3 else 1
                    if rng.random() < .15:
                        b += dur; continue
                    m = int(rng.choice([c + 12 for c in ch] + [ch[0] + 24]))
                    m = m if m >= 62 else m + 12
                    n = int(dur * beat * SR * .96); t = np.arange(n) / SR
                    f0 = _midi(m) * (1 + .012 * np.sin(2 * np.pi * 5.3 * t) * np.clip(t * 3, 0, 1))
                    ph = 2 * np.pi * np.cumsum(f0) / SR
                    vowel = [(700, 1200, 2600), (300, 2300, 3000), (500, 900, 2500)][rng.integers(3)]
                    x = np.zeros(n)
                    for k in range(1, 30):
                        fk = f0 * k
                        amp = sum(np.exp(-0.5 * ((fk - F) / (80 + .08 * F)) ** 2) * g
                                  for F, g in zip(vowel, (1, .6, .35))) + .02
                        x += np.sin(k * ph) * amp * (fk < 9000)
                    add(vox, x * _env(n, .03, 2.5) * (1 - np.exp(-(n / SR - t) * 30)), t0 + b * beat, 0, .22)
                    b += dur
            t_bar += bar
    stems = {"drums": drums, "bass": bass, "piano": piano, "guitar": guitar}
    return stems, vox


def damage_like_separator(stems, vox, strength=0.95, bleed_stem="guitar", bleed_gain=0.12, seed=1):
    """Hand instrument energy to the vocal stem wherever the vocal dominated (what separators do)."""
    inst = sum(stems.values())
    out, voc_est = {}, vox.copy()
    for c in range(2):
        Vc = stft(vox[c]); Ic = stft(inst[c])
        r = np.abs(Vc) ** 2 / (np.abs(Vc) ** 2 + np.abs(Ic) ** 2 + EPS)
        keep = 1 - strength * r ** 0.6
        n = vox.shape[1]
        for k, s in stems.items():
            out.setdefault(k, np.zeros_like(s))
            y = istft(stft(s[c]) * keep, n)
            out[k][c] = y
            voc_est[c] += s[c] - y
    # a little vocal bleed left in one instrument during the second half
    if bleed_stem in out:
        half = vox.shape[1] // 2
        out[bleed_stem][:, half:] += bleed_gain * vox[:, half:]
        voc_est[:, half:] -= bleed_gain * vox[:, half:]
    out["vocals"] = voc_est
    return out


def make_demo():
    stems, vox = synth_song()
    damaged = damage_like_separator(stems, vox)
    original = sum(stems.values()) + vox
    return damaged, original, stems
