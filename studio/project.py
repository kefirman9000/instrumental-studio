"""A song project: stems, analysis, suggestions (repairs / bleed cleanup), levels, undo, save, export."""
import json
import os
import shutil
import threading
import time

import numpy as np

from . import analysis as A
from .audio import EPS, HOP, N_FFT, load_audio, save_audio, stft, log
from .repair import match_window, refine_lag, repair_window, bleed_window, smooth_window, MARGIN, XFADE
from . import glitch as GL

DISPLAY_BANDS = 96
DS = 4                      # display columns = analysis frames / DS  (~21.5 columns per second)
STEM_ORDER = ["drums", "bass", "guitar", "piano", "other", "instrumental"]
STEM_COLORS = {"drums": "#E8875B", "bass": "#5B8DEF", "guitar": "#4FB286", "piano": "#C46BD6",
               "other": "#D9B44A", "instrumental": "#5BC0D9", "vocals": "#E05C7A"}

DEFAULTS = {
    "auto_conf": 0.6,          # repairs at/above this match are marked "recommended" (never applied without asking)
    "review_conf": 0.3,        # below this a repair is marked "no good match"
    "min_severity": 0.02,      # ignore windows with less estimated loss than this
    "vocal_threshold": 0.08,   # beats with less vocal than this are treated as vocal-free
    "n_donors": 3,
    "beats_per_bar": 4,
}


def _to_u8(x, lo=-80.0, hi=0.0):
    return np.clip((x - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)


class Project:
    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self.meta = {"name": os.path.basename(root), "settings": dict(DEFAULTS), "created": time.time()}
        self.stems, self.sr = {}, 44100
        self.original = None
        self.suggestions = {}
        self.patches = {}          # id -> (lo, float16 patch[2, n])
        self.history = []
        self.mixer = {"stem_gain": {}, "mute": {}, "solo": {}, "section_gain": {}, "keep": [], "vocal_lift": {}, "mute_ranges": []}
        self._render_cache = {}
        self.display = {}

    # ------------------------------------------------------------------ creation
    @property
    def instruments(self):
        names = [k for k in self.stems if k != "vocals"]
        return sorted(names, key=lambda k: (STEM_ORDER.index(k) if k in STEM_ORDER else 99, k))

    @property
    def stage(self):
        return self.meta.get("stage", "detected" if self.meta.get("analyzed") else "split" if self.stems else "imported")

    def _display_spec(self, x):
        cols = (x.shape[-1] // HOP + 1) // DS
        S = np.abs(stft(x.mean(0) if x.ndim == 2 else x)) ** 2
        spec = 10 * np.log10(self.Bd @ S + 1e-12)
        return spec[:, :cols * DS].reshape(DISPLAY_BANDS, cols, DS).mean(2).astype(np.float32)

    def set_original(self, original, sr, source=None, progress=lambda f, m: None):
        """Step 1: bring a song in. Nothing is split or analysed yet; it can be played straight away."""
        self.sr = sr
        self.original = np.ascontiguousarray(original, np.float32)
        n = self.original.shape[1]
        self.meta.update(duration=n / sr, sr=sr, source=source, stage="imported", stems=[])
        os.makedirs(os.path.join(self.root, "stems"), exist_ok=True)
        np.save(os.path.join(self.root, "stems", "_original.npy"), self.original)
        self.B, self.centers = A.band_matrix(sr, 40)
        self.Bd, _ = A.band_matrix(sr, DISPLAY_BANDS, 30, 16000)
        progress(0.5, "Drawing the song")
        spec = self._display_spec(self.original)
        self.n_frames = n // HOP + 1
        self.display = {"cols": spec.shape[1], "fps": sr / HOP / DS, "raw": {"original": spec}, "after": {},
                        "damage": {}, "ref_db": float(spec.max())}
        self.beats, self.beat_times, self.VA, self.windows, self.sections = np.zeros(0, np.int64), [], np.zeros(0), [], []
        self.suggestions, self.patches, self.grid, self.stem_summary = {}, {}, {}, {}
        self.save()

    def set_stems(self, stems, sr, original=None, source=None):
        self.stems, self.sr = stems, sr
        n = min(a.shape[1] for a in stems.values())
        self.stems = {k: np.ascontiguousarray(v[:, :n], np.float32) for k, v in stems.items()}
        if original is not None:
            original = original[:, :n] if original.shape[1] >= n else np.pad(original, ((0, 0), (0, n - original.shape[1])))
            self.original = original.astype(np.float32)
        else:
            self.original = sum(self.stems.values()).astype(np.float32)
        self.meta["duration"] = n / sr
        self.meta["sr"] = sr
        self.meta["source"] = source
        self.meta["stems"] = self.instruments + ["vocals"]
        self.meta["stage"] = "split"
        self._render_cache = {}
        os.makedirs(os.path.join(self.root, "stems"), exist_ok=True)
        for k, v in self.stems.items():
            np.save(os.path.join(self.root, "stems", k + ".npy"), v)
        np.save(os.path.join(self.root, "stems", "_original.npy"), self.original)

    # ------------------------------------------------------------------ analysis
    def analyze(self, progress=lambda f, m: None):
        """Prepare and detect in one go (used by tests)."""
        with self.lock:
            self._prepare(lambda f, m: progress(f * 0.3, m))
            self._detect(lambda f, m: progress(0.3 + 0.7 * f, m))

    def prepare(self, progress=lambda f, m: None):
        """Step 2b: after splitting, find beats, sections and per-instrument fingerprints. No repairs yet."""
        with self.lock:
            self._prepare(progress)

    def detect(self, progress=lambda f, m: None):
        """Step 3: find the damage and propose repairs. Replaces any earlier detection."""
        with self.lock:
            self._detect(progress)

    def _prepare(self, progress):
        sr, st = self.sr, self.meta["settings"]
        inst_names = self.instruments
        voc = self.stems["vocals"]
        inst_sum = sum(self.stems[k] for k in inst_names)
        self.inst_sum = inst_sum
        progress(0.05, "Measuring where the vocals were")
        Ic = stft(inst_sum.mean(0))
        Vc = stft(voc.mean(0))
        B, centers = A.band_matrix(sr, 40)
        self.B, self.centers = B, centers
        Bd, _ = A.band_matrix(sr, DISPLAY_BANDS, 30, 16000)
        self.Bd = Bd
        Ch = A.chroma_matrix(sr)
        I2, V2 = np.abs(Ic) ** 2, np.abs(Vc) ** 2
        r_bins = (V2 / (V2 + I2 + EPS)).astype(np.float32)
        Ib, Vb = B @ I2, B @ V2
        Mb = B @ (np.abs(Ic + Vc) ** 2)
        del Ic, Vc
        floor = 1e-3 * np.median(Ib.sum(0) + Vb.sum(0)) + EPS
        r_band = (Vb / (Vb + Ib + floor / 40)).astype(np.float32)
        sel = (centers > 100) & (centers < 8000)
        va = Vb[sel].sum(0) / (Vb[sel].sum(0) + Ib[sel].sum(0) + floor)
        n_frames = I2.shape[1]
        self.n_frames = n_frames
        fps = sr / HOP

        progress(0.25, "Finding the beat")
        env = A.onset_envelope(Mb)
        period = A.estimate_period(env, fps, self.meta.get("bpm_override"))
        beats = A.track_beats(env, period)
        low_env = A.onset_envelope(Mb[centers < 150])
        inst_chroma = Ch @ (np.sqrt(I2) * (1 - r_bins))
        bpb = st["beats_per_bar"]
        phase = A.downbeat_phase(beats, low_env, inst_chroma, bpb)
        self.beats = beats
        self.beat_times = (beats * HOP / sr).tolist()
        self.meta["bpm"] = float(60 * fps / np.median(np.diff(beats)))
        nb = len(beats) - 1
        VA = np.array([va[beats[j]:max(beats[j + 1], beats[j] + 1)].mean() for j in range(nb)])
        self.VA = VA
        # bar windows
        windows = []
        if phase > 0:
            windows.append((0, phase))
        s = phase
        while s < nb:
            windows.append((s, min(bpb, nb - s)))
            s += bpb
        self.windows = windows

        progress(0.4, "Detecting song sections")
        bar_feats, bar_voc, bar_en = [], [], []
        Ilog = np.log(Ib + floor / 40)
        for s, L in windows:
            a, b = beats[s], beats[s + L]
            c = inst_chroma[:, a:b].mean(1)
            c = c / (np.linalg.norm(c) + EPS)
            e = Ilog[:, a:b].mean(1)
            e = (e - e.mean()) / 6.0
            bar_feats.append(np.concatenate([2.0 * c, e]))
            bar_voc.append(VA[s:s + L].mean())
            bar_en.append(np.log(Ib[:, a:b].sum(0).mean() + EPS))
        secs = A.detect_sections(np.array(bar_feats), np.array(bar_voc), np.array(bar_en))
        for sec in secs:
            sec["start"] = float(beats[windows[sec["start_bar"]][0]] * HOP / sr)
            last = windows[sec["end_bar"] - 1]
            sec["end"] = float(beats[last[0] + last[1]] * HOP / sr)
        self.sections = secs

        # per-stem analysis
        self.stem_feats, self.stem_env = {}, {}
        disp_cols = n_frames // DS
        self.display = {"cols": int(disp_cols), "fps": fps / DS, "raw": {}, "after": {}, "damage": {}}
        vband_presence = r_band
        for i, k in enumerate(inst_names + ["vocals"]):
            progress(0.5 + 0.45 * i / (len(inst_names) + 1), f"Analysing {k}")
            Sc = stft(self.stems[k].mean(0))
            S = np.abs(Sc)
            del Sc
            spec = 10 * np.log10(Bd @ (S ** 2) + 1e-12)
            spec = spec[:, :disp_cols * DS].reshape(DISPLAY_BANDS, disp_cols, DS).mean(2)
            self.display["raw"][k] = spec.astype(np.float32)
            if k == "vocals":
                continue
            Sb = B @ (S ** 2)
            chroma = Ch @ (S * (1 - r_bins))
            F, C, R = A.beat_features(Sb, chroma, vband_presence, beats)
            self.stem_feats[k] = (F, C, R)
            self.stem_env[k] = A.onset_envelope(Sb)
            self.display["damage"][k] = np.zeros((DISPLAY_BANDS, disp_cols), np.float32)
        self.display["raw"]["original"] = self._display_spec(self.original)
        ref = max(float(v.max()) for v in self.display["raw"].values())
        self.display["ref_db"] = ref
        del r_bins
        self.suggestions, self.patches, self.history = {}, {}, []
        self.display["after"] = {k: self.display["raw"][k].copy() for k in inst_names}
        self.meta["stage"] = "split"
        self.meta["analyzed"] = False
        self._compute_grid()
        self.save()
        progress(1.0, "Done")

    def _detect(self, progress):
        inst_names = self.instruments
        windows = self.windows
        disp_cols = self.display["cols"]
        progress(0.01, "Getting ready")
        self.inst_sum = sum(self.stems[k] for k in inst_names)
        for k in inst_names:
            self.display["damage"][k] = np.zeros((DISPLAY_BANDS, disp_cols), np.float32)
            self._render_cache.pop(("stem", k), None)
        self.suggestions, self.patches, self.history = {}, {}, []
        self.level_floor = {}
        jobs = [(k, w) for k in inst_names for w in range(len(windows))]
        total = max(len(jobs), 1)
        # windows are independent; NumPy/SciPy release the GIL in the heavy parts, so threads scale
        from concurrent.futures import ThreadPoolExecutor
        for k in inst_names:
            self._level_floor(k)
        workers = max(1, min(8, (os.cpu_count() or 2)))
        with ThreadPoolExecutor(workers) as pool:
            for n_done, _ in enumerate(pool.map(lambda kw: self._make_window_suggestions(*kw), jobs)):
                if n_done % 8 == 0:
                    progress(0.02 + 0.9 * n_done / total, f"Checking {jobs[n_done][0]} for damage")
        progress(0.93, "Scoring sections")
        for k in inst_names:
            self._render_cache.pop(("stem", k), None)
            self._refresh_after(k, 0, self.stems[k].shape[1])
        self._compute_grid()
        self.meta["analyzed"] = True
        self.meta["stage"] = "detected"
        progress(0.97, "Listening for clicks and dropouts")
        self.scan_glitches()
        self.save()
        progress(1.0, "Done")

    def _level_floor(self, k):
        if k not in self.level_floor:
            x = self.stems[k].mean(0)
            self.level_floor[k] = float(np.sqrt(np.mean(x ** 2)) * N_FFT * 0.5 * 10 ** (-60 / 20) + EPS)
        return self.level_floor[k]

    def _window_samples(self, s, L):
        return int(self.beats[s] * HOP), int(self.beats[s + L] * HOP)

    def _section_of(self, t):
        for i, sec in enumerate(self.sections):
            if sec["start"] - 1e-6 <= t < sec["end"]:
                return i
        return len(self.sections) - 1

    def _make_window_suggestions(self, k, wi):
        st = self.meta["settings"]
        s, L = self.windows[wi]
        if self.VA[s:s + L].max() < st["vocal_threshold"]:
            return
        F, C, R = self.stem_feats[k]
        s0, s1 = self._window_samples(s, L)
        seg = self.stems[k][:, s0:s1]
        if seg.size == 0 or np.sqrt(np.mean(seg ** 2)) < 1e-5 and np.abs(self.stems[k]).max() > 0:
            # silent here; only worth a look if the instrument normally plays in this kind of bar
            pass
        # vocal coverage of this instrument's usual spectrum = risk even without a donor
        prof = np.exp(F).mean(0)
        risk = float((prof * R[s:s + L].mean(0)).sum() / (prof.sum() + EPS))
        donors, conf = match_window(F, C, R, s, L, st["n_donors"])
        sid = f"r_{k}_{wi}"
        base = {"id": sid, "type": "repair", "stem": k, "window": wi, "beat": s, "beats": L,
                "start": s0 / self.sr, "end": s1 / self.sr, "section": self._section_of(s0 / self.sr),
                "risk": risk, "strength": 1.0, "vocal": float(self.VA[s:s + L].mean())}
        if not donors:
            if risk > 0.1:
                self.suggestions[sid] = {**base, "status": "nodonor", "conf": 0.0, "severity": risk * 0.5,
                                         "donors": [], "note": "No similar vocal-free passage found"}
            return
        res = self._run_repair(k, s, L, donors, conf, 1.0)
        if res["severity"] < st["min_severity"] and res["added_db"] < -30:
            return
        # nothing is applied until the person says so
        status = "pending" if conf >= st["review_conf"] else "low"
        self.suggestions[sid] = {**base, "status": status, "conf": conf, "severity": res["severity"],
                                 "added_db": res["added_db"], "damage_area": res["damage_area"],
                                 "donors": self._donor_info(donors, L), "manual_donor": False,
                                 "recommended": conf >= st["auto_conf"]}
        self._store_patch(sid, k, res)
        # vocal bleed check in the same window
        b = bleed_window(self.stems[k], self.stems["vocals"], s0, s1)
        if b is not None:
            bid = f"b_{k}_{wi}"
            self.suggestions[bid] = {**base, "id": bid, "type": "bleed", "status": "pending", "conf": b["score"],
                                     "severity": b["score"], "removed_db": b["removed_db"],
                                     "note": "Leftover vocal detected in this instrument"}
            self.patches[bid] = (b["lo"], b["patch"].astype(np.float16))

    def _donor_info(self, donors, L):
        out = []
        for d in donors:
            a, b = self._window_samples(d["beat"], L)
            out.append({"beat": d["beat"], "start": a / self.sr, "end": b / self.sr,
                        "section": self._section_of(a / self.sr), "match": float(np.clip(1 - d["dist"], 0, 1)),
                        "vocal_overlap": d["overlap"]})
        return out

    def _run_repair(self, k, s, L, donors, conf, strength):
        s0, s1 = self._window_samples(s, L)
        b0, b1 = self.beats[s], self.beats[s + L]
        lags = [refine_lag(self.stem_env[k], b0, b1, self.beats[d["beat"]]) for d in donors]
        return repair_window(self.stems[k], self.stems["vocals"], self.inst_sum, s0, s1, lags, conf, strength,
                             self.B, self.centers, self.sr, self._level_floor(k))

    def _store_patch(self, sid, k, res):
        self.patches[sid] = (res["lo"], res["patch"].astype(np.float16))
        D = res["D"]
        f_lo = int(round(res["lo"] / HOP))
        dm = self.Bd @ D
        cols = self.display["cols"]
        sug = self.suggestions[sid]
        c0 = int(sug["start"] * self.sr / HOP / DS)
        c1 = int(np.ceil(sug["end"] * self.sr / HOP / DS))
        for c in range(max(c0, 0), min(c1, cols)):
            f = c * DS - f_lo
            if 0 <= f < dm.shape[1] - DS:
                self.display["damage"][k][:, c] = dm[:, f:f + DS].mean(1)

    # ------------------------------------------------------------------ rendering
    def _applied(self, sug):
        return sug["status"] in ("auto", "accepted")

    def render_stem(self, k, version="repaired"):
        if version == "raw" or k == "vocals":
            return self.stems[k]
        key = ("stem", k)
        if key in self._render_cache:
            return self._render_cache[key]
        out = self.stems[k].copy()
        n = out.shape[1]
        delta = np.zeros_like(out)
        for sid, sug in self.suggestions.items():
            if sug["stem"] != k or not self._applied(sug) or sid not in self.patches:
                continue
            lo, p = self.patches[sid]
            a, b = max(lo, 0), min(lo + p.shape[1], n)
            if b > a:
                delta[:, a:b] += p[:, a - lo:b - lo].astype(np.float32)
        kept = self.keep_mask(0, n)
        if kept is not None:          # kept parts are never repaired
            delta *= (1 - kept)[None, :]
        out += delta
        self._render_cache[key] = out
        return out

    # ------------------------------------------------------------------ kept parts
    def keep_ranges(self, mode=None):
        return [r for r in self.mixer.get("keep", []) if mode is None or r.get("mode") == mode]

    def keep_mask(self, a, b, mode=None, ramp=0.03):
        """0..1 per sample for [a, b): 1 inside kept parts, with short raised-cosine edges."""
        ranges = self.keep_ranges(mode)
        if not ranges:
            return None
        t = np.arange(a, b, dtype=np.float64) / self.sr
        m = np.zeros(b - a, np.float32)
        for r in ranges:
            up = np.clip((t - r["start"] + ramp / 2) / ramp, 0, 1)
            dn = np.clip((r["end"] + ramp / 2 - t) / ramp, 0, 1)
            m = np.maximum(m, (np.sin(0.5 * np.pi * np.minimum(up, dn)) ** 2).astype(np.float32))
        return m

    def mute_mask(self, a, b, stem=None, ramp=0.02, only_all=False):
        """1 where muted: ranges for this instrument (stem) or for everything (range stem None)."""
        rs = [r for r in self.mixer.get("mute_ranges", [])
              if r.get("stem") is None or (not only_all and r.get("stem") == stem)]
        if not rs:
            return None
        t = np.arange(a, b, dtype=np.float64) / self.sr
        m = np.zeros(b - a, np.float32)
        for r in rs:
            up = np.clip((t - r["start"] + ramp / 2) / ramp, 0, 1)
            dn = np.clip((r["end"] + ramp / 2 - t) / ramp, 0, 1)
            m = np.maximum(m, np.minimum(up, dn).astype(np.float32))
        return m

    def _vocal_curve(self, a, b):
        """How much singing there is at each sample, 0..1, from the per-beat vocal measure."""
        if not len(self.VA):
            return np.zeros(b - a, np.float32)
        mids = np.array([(self.beat_times[j] + self.beat_times[j + 1]) / 2 for j in range(len(self.VA))])
        v = np.clip(np.asarray(self.VA) / 0.25, 0, 1)
        return np.interp(np.arange(a, b) / self.sr, mids, v).astype(np.float32)

    def _refresh_after(self, k, a, b):
        if not self.display:
            return
        cols = self.display["cols"]
        c0, c1 = max(0, a // HOP // DS - 2), min(cols, b // HOP // DS + 3)
        if c1 <= c0:
            return
        y = self.render_stem(k)
        s0, s1 = c0 * DS * HOP, c1 * DS * HOP
        seg = y[:, max(s0 - N_FFT, 0):s1 + N_FFT].mean(0)
        off = (s0 - max(s0 - N_FFT, 0)) // HOP
        S = np.abs(stft(seg)) ** 2
        spec = 10 * np.log10(self.Bd @ S + 1e-12)
        spec = spec[:, off:off + (c1 - c0) * DS]
        m = spec.shape[1] // DS
        if k not in self.display["after"]:
            self.display["after"][k] = self.display["raw"][k].copy()
        self.display["after"][k][:, c0:c0 + m] = spec[:, :m * DS].reshape(DISPLAY_BANDS, m, DS).mean(2)

    def _invalidate(self, k, sid=None):
        self._render_cache.pop(("stem", k), None)
        self._render_cache.pop("mix", None)
        if sid and sid in self.patches:
            lo, p = self.patches[sid]
            self._refresh_after(k, lo, lo + p.shape[1])

    def mix(self, version="repaired", region=None, solo_stem=None, use_mixer=True):
        """version: repaired | raw | original | vocals | patch."""
        if version == "original" or not self.instruments:
            out = self.original
        elif version == "vocals":
            out = self.stems["vocals"]
        else:
            stems = [solo_stem] if solo_stem else self.instruments
            m = self.mixer
            any_solo = use_mixer and not solo_stem and any(m["solo"].get(k) for k in stems)
            n = self.stems["vocals"].shape[1]
            a, b = region if region else (0, n)
            out = np.zeros((2, b - a), np.float32)
            for k in stems:
                if use_mixer and not solo_stem:
                    if m["mute"].get(k) or (any_solo and not m["solo"].get(k)):
                        continue
                if version == "patch":
                    y = self.shaped_stem(k, a, b) - self.stems[k][:, a:b]
                else:
                    y = self.shaped_stem(k, a, b, version)
                g = self._gain_curve(k, a, b) if use_mixer else 1.0
                out += y * g
            if version == "repaired" and use_mixer and not solo_stem and not any_solo \
                    and not any(m["mute"].get(k) for k in stems):
                mo = self.keep_mask(a, b, "original")
                if mo is not None:
                    out = out * (1 - mo)[None, :] + self.original[:, a:b] * mo[None, :]
                mall = self.mute_mask(a, b, only_all=True)
                if mall is not None:
                    out = out * (1 - mall)[None, :]
            return out
        return out[:, region[0]:region[1]] if region else out

    LIFT_BAND = (250.0, 5000.0)   # where vocals cover instruments, so where separation thins them

    def shaped_stem(self, k, a=0, b=None, version="repaired"):
        """Rendered stem for [a, b) including the 'fill under the vocals' tone lift, if one is set."""
        y = self.render_stem(k, version)
        b = y.shape[1] if b is None else b
        seg = y[:, a:b]
        lift = float(self.mixer.get("vocal_lift", {}).get(k, 0.0)) if version == "repaired" else 0.0
        if not lift:
            return seg
        from scipy import signal as sig
        pad = int(0.2 * self.sr)
        lo, hi = max(0, a - pad), min(y.shape[1], b + pad)
        sos = sig.butter(2, self.LIFT_BAND, "bandpass", fs=self.sr, output="sos")
        band = sig.sosfiltfilt(sos, y[:, lo:hi], axis=1)[:, a - lo:a - lo + (b - a)].astype(np.float32)
        amount = (10 ** (lift / 20) - 1) * self._vocal_curve(a, b)
        kept = self.keep_mask(a, b)
        if kept is not None:
            amount *= (1 - kept)
        return seg + band * amount[None, :]

    def remaining_loss(self, k):
        """Estimated dB the instrument is still missing under the vocals, measured against its matched
        sources (same part, vocal-free) so arrangement changes cancel out."""
        if self.stage != "detected" or k not in self.stem_feats:
            return 0.0
        y = self.shaped_stem(k)
        E = self.B @ (np.abs(stft(y.mean(0))) ** 2)
        F, _, _ = A.beat_features(E, np.zeros((12, E.shape[1]), np.float32), np.zeros_like(E), self.beats)
        _, _, R = self.stem_feats[k]
        vals, wts = [], []
        for x in self.suggestions.values():
            if x["stem"] != k or x["type"] != "repair" or not x.get("donors"):
                continue
            a, L, dn = x["beat"], x["beats"], x["donors"][0]["beat"]
            Rt, Rd, Ft, Fd = R[a:a + L], R[dn:dn + L], F[a:a + L], F[dn:dn + L]
            rel, cov = (1 - Rt) * (1 - Rd), Rt * (1 - Rd)
            if cov.sum() < 0.5 or rel.sum() < 0.5:
                continue
            off = ((Fd - Ft) * rel).sum() / rel.sum()
            vals.append((((Fd - Ft) * cov).sum() / cov.sum() - off) * 10 / np.log(10))
            wts.append(cov.sum())
        return float(np.average(vals, weights=wts)) if vals else 0.0

    def _gain_curve(self, k, a, b):
        m = self.mixer
        g0 = 10 ** (float(m["stem_gain"].get(k, 0.0)) / 20)
        sg = m["section_gain"]
        muted = self.mute_mask(a, b, k)
        if not any(key.endswith(":" + k) and v for key, v in sg.items()):
            return g0 if muted is None else (g0 * (1 - muted))[None, :]
        t = (np.arange(a, b) / self.sr)
        curve = np.ones(b - a, np.float32)
        ramp = 0.03
        for i, sec in enumerate(self.sections):
            db = float(sg.get(f"{i}:{k}", 0.0))
            if not db:
                continue
            gi = 10 ** (db / 20) - 1
            up = np.clip((t - sec["start"] + ramp / 2) / ramp, 0, 1)
            dn = np.clip((sec["end"] + ramp / 2 - t) / ramp, 0, 1)
            curve += gi * np.minimum(up, dn)
        kept = self.keep_mask(a, b)
        if kept is not None:          # kept parts keep their original level
            curve = kept + (1 - kept) * curve
        if muted is not None:
            curve = curve * (1 - muted)
        return (g0 * curve)[None, :]

    def preview(self, sid, mode="after", context=1.5, hear="mix"):
        """Audio around one suggested edit.

        mode: before | after | patch | donor | original
        hear: solo  - just this instrument
              mix   - all instruments, as they currently sound (your other repairs and levels included)
              song  - all instruments plus the vocals, to judge the edit in the finished song's context
        Before and after differ only by this one edit, whatever else is on.
        """
        sug = self.suggestions[sid]
        n = self.original.shape[1]
        a = int(max(0, (sug["start"] - context) * self.sr))
        b = int(min(n, (sug["end"] + context) * self.sr))
        k = sug["stem"]
        if mode == "original":
            return self.original[:, a:b]
        if mode == "donor" and sug.get("donors"):
            d = sug["donors"][0]
            da = int(max(0, (d["start"] - context) * self.sr))
            db = min(n, da + (b - a))
            if hear == "solo":
                return self.stems[k][:, da:db]
            y = self.mix("repaired", region=(da, db))
            return y + self.stems["vocals"][:, da:db] if hear == "song" else y

        # this edit alone, as it would sound in the output (kept parts block it, levels apply to it)
        lo, p = self.patches.get(sid, (a, np.zeros((2, 0), np.float16)))
        delta = np.zeros((2, b - a), np.float32)
        pa, pb = max(lo, a), min(lo + p.shape[1], b)
        if pb > pa:
            delta[:, pa - a:pb - a] = p[:, pa - lo:pb - lo].astype(np.float32)
        kept = self.keep_mask(a, b)
        if kept is not None:
            delta *= (1 - kept)[None, :]
        if mode == "patch":
            return delta if hear == "solo" else delta * self._gain_curve(k, a, b)

        if hear == "solo":
            base = self.stems[k][:, a:b] + (self.render_stem(k)[:, a:b] - self.stems[k][:, a:b])
            this = delta
        else:
            base = self.mix("repaired", region=(a, b))       # everything as it is right now
            this = delta * self._gain_curve(k, a, b)
            if hear == "song":
                vocals = self.stems["vocals"][:, a:b]
                mo = self.keep_mask(a, b, "original")      # the original recording already has the vocals
                base = base + (vocals if mo is None else vocals * (1 - mo)[None, :])
        applied = self._applied(sug)
        before = base - this if applied else base
        after = base if applied else base + this
        return before if mode == "before" else after

    # ------------------------------------------------------------------ edits
    def _snapshot(self, ids):
        return [{"id": i, "sug": json.loads(json.dumps(self.suggestions[i])),
                 "patch": self.patches.get(i)} for i in ids if i in self.suggestions]

    def _push(self, label, ids):
        self.history.append({"label": label, "items": self._snapshot(ids), "mixer": json.loads(json.dumps(self.mixer))})
        self.history = self.history[-200:]

    def set_status(self, ids, status, label=None):
        with self.lock:
            ids = [i for i in ids if i in self.suggestions and self.suggestions[i]["status"] != "nodonor"]
            if not ids:
                return
            self._push(label or f"{status} {len(ids)} edit(s)", ids)
            for i in ids:
                self.suggestions[i]["status"] = status
            spans = {}
            for i in ids:          # one display refresh per instrument, not one per repair
                k = self.suggestions[i]["stem"]
                self._render_cache.pop(("stem", k), None)
                if i in self.patches:
                    lo, p = self.patches[i]
                    a, b = spans.get(k, (lo, lo + p.shape[1]))
                    spans[k] = (min(a, lo), max(b, lo + p.shape[1]))
            self._render_cache.pop("mix", None)
            for k, (a, b) in spans.items():
                self._refresh_after(k, a, b)
            self._compute_grid()

    def set_strength(self, sid, strength):
        with self.lock:
            sug = self.suggestions[sid]
            self._push(f"Strength {sug['stem']} {sug['start']:.1f}s", [sid])
            sug["strength"] = float(strength)
            if sug["type"] == "repair":
                donors = [{"beat": d["beat"], "dist": 1 - d["match"], "overlap": d["vocal_overlap"]} for d in sug["donors"]]
                res = self._run_repair(sug["stem"], sug["beat"], sug["beats"], donors, sug["conf"], strength)
                self._store_patch(sid, sug["stem"], res)
                sug["added_db"] = res["added_db"]
            elif sug["type"] == "smooth":
                s0, s1 = int(sug["start"] * self.sr), int(sug["end"] * self.sr)
                res = smooth_window(self.stems[sug["stem"]], self.stems["vocals"], self.inst_sum, s0, s1, strength)
                self.patches[sid] = (res["lo"], res["patch"].astype(np.float16))
            elif sug["type"] == "bleed":
                s0, s1 = self._window_samples(sug["beat"], sug["beats"])
                b = bleed_window(self.stems[sug["stem"]], self.stems["vocals"], s0, s1, strength)
                if b is not None:
                    self.patches[sid] = (b["lo"], b["patch"].astype(np.float16))
            self._invalidate(sug["stem"], sid)
            self._compute_grid()

    # ------------------------------------------------------------------ glitches, smoothing
    def scan_glitches(self, start=None, end=None):
        """Look for clicks and dropouts in every instrument as it sounds now. Adds review items;
        changes nothing. Replaces earlier undecided glitch items in the same range."""
        with self.lock:
            n = self.original.shape[1]
            a = 0 if start is None else max(0, int(start * self.sr))
            b = n if end is None else min(n, int(end * self.sr))
            for sid in [i for i, x in self.suggestions.items() if x["type"] == "glitch" and x["status"] == "pending"
                        and x["end"] > a / self.sr and x["start"] < b / self.sr]:
                self.suggestions.pop(sid, None)
                self.patches.pop(sid, None)
            vc = self._vocal_curve(0, n)
            beats = np.asarray(self.beats) * HOP
            orig = self.original.mean(0)
            found = 0
            for k in self.instruments:
                y = self.render_stem(k)
                for g in GL.scan(y[:, a:b], self.sr, k, self.stems[k][:, a:b],
                                 lambda i, a=a: vc[min(a + i, n - 1)], orig[a:b], beats - a):
                    s0, s1 = a + g["start"], a + g["end"]
                    sid = f"g_{k}_{s0}"
                    t0, t1 = s0 / self.sr, s1 / self.sr
                    self.suggestions[sid] = {
                        "id": sid, "type": "glitch", "kind": g["kind"], "stem": k, "status": "pending",
                        "start": max(0.0, t0 - 0.05), "end": t1 + 0.05, "section": self._section_of(t0),
                        "conf": 1.0, "severity": 0.0, "strength": 1.0, "donors": [],
                        "note": "A short click or pop" if g["kind"] == "click" else f"A {1000 * (t1 - t0):.0f} ms gap in the sound"}
                    self.patches[sid] = (a + g["lo"], g["patch"].astype(np.float16))
                    found += 1
            self.meta["glitch_scan_applied"] = self._applied_count()
            self._compute_grid()
            return found

    def _applied_count(self):
        return sum(1 for x in self.suggestions.values() if x["type"] != "glitch" and self._applied(x))

    def smooth_range(self, start, end, stems=None):
        """Offer to calm 'watery' separation sound under the vocals in [start, end), one item per
        instrument and bar. Nothing is applied until the person applies it."""
        with self.lock:
            ids = []
            for k in (stems or self.instruments):
                for wi, (s, L) in enumerate(self.windows):
                    s0, s1 = self._window_samples(s, L)
                    if s1 / self.sr <= start or s0 / self.sr >= end:
                        continue
                    s0, s1 = max(s0, int(start * self.sr)), min(s1, int(end * self.sr))
                    if s1 - s0 < self.sr // 10:
                        continue
                    sid = f"s_{k}_{s0}"
                    res = smooth_window(self.stems[k], self.stems["vocals"], self.inst_sum, s0, s1)
                    if float(np.abs(res["patch"]).max()) < 1e-4:
                        continue                      # no singing there, nothing to smooth
                    self.suggestions[sid] = {
                        "id": sid, "type": "smooth", "stem": k, "status": "pending", "start": s0 / self.sr,
                        "end": s1 / self.sr, "section": self._section_of(s0 / self.sr), "conf": 1.0, "severity": 0.0,
                        "strength": 1.0, "donors": [], "note": "Calms the watery, warbling sound separation leaves under the vocals"}
                    self.patches[sid] = (res["lo"], res["patch"].astype(np.float16))
                    ids.append(sid)
            return ids

    def donor_candidates(self, sid, n=8):
        sug = self.suggestions[sid]
        F, C, R = self.stem_feats[sug["stem"]]
        donors, _ = match_window(F, C, R, sug["beat"], sug["beats"], n_donors=n)
        return self._donor_info(donors, sug["beats"])

    def set_donor(self, sid, beat=None, time_s=None):
        """Manually choose where a repair borrows from (by beat index or by time in seconds)."""
        with self.lock:
            sug = self.suggestions[sid]
            if beat is None:
                beat = int(np.argmin(np.abs(np.array(self.beat_times[:-1]) - float(time_s))))
            L = sug["beats"]
            beat = int(np.clip(beat, 0, len(self.beats) - 1 - L))
            self._push(f"Donor for {sug['stem']} {sug['start']:.1f}s", [sid])
            F, C, R = self.stem_feats[sug["stem"]]
            # score this donor with the same yardstick
            ranked, _ = match_window(F, C, R, sug["beat"], L, n_donors=len(F))
            hit = next((d for d in ranked if d["beat"] == beat), None)
            d = hit or {"beat": beat, "dist": 0.9, "overlap": float((R[sug['beat']:sug['beat'] + L] * R[beat:beat + L]).sum() / (R[sug['beat']:sug['beat'] + L].sum() + EPS))}
            conf = float(max(0.5, np.clip(1.3 * (1 - d["dist"]), 0, 1)))  # user picked it: trust it more
            res = self._run_repair(sug["stem"], sug["beat"], L, [d], conf, sug["strength"])
            sug.update({"donors": self._donor_info([d], L), "conf": conf, "manual_donor": True,
                        "severity": res["severity"], "added_db": res["added_db"],
                        "status": sug["status"] if sug["status"] in ("accepted", "rejected") else "pending",
                        "type": "repair", "note": "Donor chosen by you"})
            self._store_patch(sid, sug["stem"], res)
            self._invalidate(sug["stem"], sid)
            self._compute_grid()

    def set_mixer(self, mixer):
        with self.lock:
            self._push("Levels", [])
            keep_changed = "keep" in mixer and mixer["keep"] != self.mixer.get("keep")
            for key in ("stem_gain", "mute", "solo", "section_gain", "keep", "vocal_lift", "mute_ranges"):
                if key in mixer:
                    self.mixer[key] = mixer[key]
            if "keep" in mixer:
                self.mixer["keep"] = self._clean_keep(self.mixer["keep"])
            self._render_cache.pop("mix", None)
            if keep_changed:
                for k in self.instruments:
                    self._render_cache.pop(("stem", k), None)
                    self._refresh_after(k, 0, self.stems[k].shape[1])
                self._compute_grid()

    def _clean_keep(self, ranges):
        """Sort, clamp and merge overlapping kept parts of the same kind."""
        dur = self.meta.get("duration", 0)
        out = []
        for r in sorted(ranges, key=lambda r: float(r["start"])):
            a, b = max(0.0, float(r["start"])), min(dur, float(r["end"]))
            mode = "original" if r.get("mode") == "original" else "no_edits"
            if b - a < 0.05:
                continue
            if out and out[-1]["mode"] == mode and a <= out[-1]["end"] + 0.01:
                out[-1]["end"] = max(out[-1]["end"], b)
            else:
                out.append({"start": a, "end": b, "mode": mode})
        return out

    def undo(self):
        with self.lock:
            if not self.history:
                return None
            h = self.history.pop()
            keep_changed = h["mixer"].get("keep") != self.mixer.get("keep")
            self.mixer = h["mixer"]
            if keep_changed:
                for k in self.instruments:
                    self._render_cache.pop(("stem", k), None)
                    self._refresh_after(k, 0, self.stems[k].shape[1])
            for it in h["items"]:
                self.suggestions[it["id"]] = it["sug"]
                if it["patch"] is not None:
                    self.patches[it["id"]] = it["patch"]
                self._invalidate(it["sug"]["stem"], it["id"])
            self._compute_grid()
            return h["label"]

    # ------------------------------------------------------------------ section x instrument grid
    def _compute_grid(self):
        grid = {}
        for si, sec in enumerate(self.sections):
            for k in self.instruments:
                sugs = [s for s in self.suggestions.values()
                        if s["stem"] == k and s["section"] == si and s["type"] == "repair"]
                dur = sec["end"] - sec["start"]
                if not sugs:
                    grid[f"{si}:{k}"] = {"damage": 0.0, "repairability": 1.0, "fixed": 1.0, "count": 0,
                                         "state": "clean" if not sec["vocal"] else "undamaged"}
                    continue
                w = np.array([max(s["severity"], 1e-3) * (s["end"] - s["start"]) for s in sugs])
                dmg = float(sum(s["severity"] * (s["end"] - s["start"]) for s in sugs) / max(dur, 1e-6))
                rep = float((w * np.array([s["conf"] for s in sugs])).sum() / w.sum())
                fixed = float((w * np.array([1.0 if self._applied(s) else 0.0 for s in sugs])).sum() / w.sum())
                state = "repaired" if fixed > 0.8 else ("repairable" if rep >= 0.55 else
                                                         "partial" if rep >= 0.3 else "hard")
                grid[f"{si}:{k}"] = {"damage": dmg, "repairability": rep, "fixed": fixed, "count": len(sugs),
                                     "state": state}
        self.grid = grid
        # per-stem summary
        self.stem_summary = {}
        for k in self.instruments:
            sugs = [s for s in self.suggestions.values() if s["stem"] == k and s["type"] == "repair"]
            dur = self.meta["duration"]
            dmg = sum(s["severity"] * (s["end"] - s["start"]) for s in sugs) / max(dur, 1e-6)
            w = np.array([max(s["severity"], 1e-3) for s in sugs]) if sugs else np.array([1.0])
            rep = float((w * np.array([s["conf"] for s in sugs])).sum() / w.sum()) if sugs else 1.0
            x = self.stems[k]
            self.stem_summary[k] = {"damage": float(dmg), "repairability": rep, "count": len(sugs),
                                    "rms_db": float(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9))}

    def section_levels(self):
        """RMS level (dBFS) of every instrument in every section, after repairs."""
        out = {}
        for si, sec in enumerate(self.sections):
            a, b = int(sec["start"] * self.sr), int(sec["end"] * self.sr)
            for k in self.instruments + ["vocals"]:
                y = self.shaped_stem(k, a, b) if k != "vocals" else self.stems[k][:, a:b]
                if k != "vocals" and y.size:
                    y = y * self._gain_curve(k, a, b)
                out[f"{si}:{k}"] = float(20 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-9)) if y.size else -120.0
        return out

    # ------------------------------------------------------------------ bulk + settings
    def bulk(self, action, stem=None, section=None, types=("repair",)):
        ids = [s["id"] for s in self.suggestions.values()
               if (stem is None or s["stem"] == stem) and (section is None or s["section"] == section)
               and s["type"] in types and s["status"] != "nodonor"]
        if action == "accept_recommended":
            ids = [i for i in ids if self.suggestions[i].get("recommended") and self.suggestions[i]["status"] == "pending"]
            return self.set_status(ids, "accepted", f"Applied {len(ids)} recommended repair(s)")
        status = {"accept": "accepted", "reject": "rejected"}.get(action)
        if action == "reset":
            st = self.meta["settings"]
            with self.lock:
                self._push("Reset to suggestions", ids)
                for i in ids:
                    s = self.suggestions[i]
                    s["status"] = "pending" if s["type"] == "bleed" else (
                        "pending" if s["conf"] >= st["review_conf"] else "low")
                    self._invalidate(s["stem"], i)
                self._compute_grid()
            return
        self.set_status(ids, status, f"{action} {len(ids)} edit(s)")

    # ------------------------------------------------------------------ export
    def export(self, folder, fmt="wav", what=("mix", "stems", "raw", "vocals", "report"), bits=24):
        os.makedirs(folder, exist_ok=True)
        name = os.path.splitext(self.meta.get("name") or "song")[0]
        ext = "." + fmt
        b = 16 if (fmt == "wav" and bits == 16) else 32
        written = []

        def put(fname, audio):
            peak = float(np.abs(audio).max()) + EPS
            if peak > 0.999:
                audio = audio * (0.999 / peak)
            p = os.path.join(folder, fname + ext)
            save_audio(p, audio, self.sr, bits=b)
            written.append(p)

        if "mix" in what:
            put(f"{name} - instrumental (repaired)", self.mix("repaired"))
        if "raw" in what:
            put(f"{name} - instrumental (raw)", self.mix("raw", use_mixer=False))
        if "stems" in what:
            for k in self.instruments:
                put(f"{name} - {k} (repaired)", self.shaped_stem(k) * self._gain_curve(k, 0, self.stems[k].shape[1]))
        if "vocals" in what:
            put(f"{name} - vocals", self.stems["vocals"])
        if "report" in what:
            p = os.path.join(folder, f"{name} - repair report.json")
            with open(p, "w") as f:
                json.dump(self.report(), f, indent=2)
            written.append(p)
        return written

    def report(self):
        secs = self.sections
        return {
            "song": self.meta.get("name"), "duration_s": self.meta.get("duration"), "bpm": self.meta.get("bpm"),
            "sections": [{"name": s["name"], "start": s["start"], "end": s["end"], "vocals": s["vocal"]} for s in secs],
            "instruments": self.stem_summary,
            "grid": {f"{secs[int(k.split(':')[0])]['name']} / {k.split(':')[1]}": v for k, v in self.grid.items()},
            "edits": [{k: v for k, v in s.items() if k not in ("window",)} for s in
                      sorted(self.suggestions.values(), key=lambda s: (s["start"], s["stem"]))],
            "levels": self.mixer,
        }

    # ------------------------------------------------------------------ persistence
    def save(self):
        with self.lock:
            os.makedirs(self.root, exist_ok=True)
            state = {"meta": self.meta, "sections": getattr(self, "sections", []),
                     "beats": getattr(self, "beats", np.zeros(0)).tolist(),
                     "windows": getattr(self, "windows", []), "VA": getattr(self, "VA", np.zeros(0)).tolist(),
                     "suggestions": self.suggestions, "mixer": self.mixer}
            tmp = os.path.join(self.root, "project.json.tmp")
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, os.path.join(self.root, "project.json"))
            np.savez(os.path.join(self.root, "patches.npz"),
                     **{f"{k}__lo": np.array([v[0]]) for k, v in self.patches.items()},
                     **{f"{k}__p": v[1] for k, v in self.patches.items()})
            if self.display:
                d = {f"{kind}__{k}": v.astype(np.float16) for kind in ("raw", "after", "damage")
                     for k, v in self.display.get(kind, {}).items()}
                np.savez(os.path.join(self.root, "display.npz"), **d)
                if hasattr(self, "stem_feats"):
                    np.savez(os.path.join(self.root, "features.npz"),
                             **{f"{k}__{i}": a for k, t in self.stem_feats.items() for i, a in enumerate(t)},
                             **{f"{k}__env": v for k, v in self.stem_env.items()})

    @classmethod
    def load(cls, root):
        p = cls(root)
        with open(os.path.join(root, "project.json")) as f:
            state = json.load(f)
        p.meta, p.sections, p.suggestions, p.mixer = state["meta"], state["sections"], state["suggestions"], state["mixer"]
        p.mixer.setdefault("keep", [])
        p.mixer.setdefault("vocal_lift", {})
        p.mixer.setdefault("mute_ranges", [])
        for x in p.suggestions.values():
            if x.get("status") == "auto":     # made by an older version without asking: ask now
                x["status"] = "pending"
                x["recommended"] = True
        p.beats = np.array(state["beats"], dtype=np.int64)
        p.windows = [tuple(w) for w in state["windows"]]
        p.VA = np.array(state["VA"])
        p.sr = p.meta.get("sr", 44100)
        p.beat_times = (p.beats * HOP / p.sr).tolist()
        sd = os.path.join(root, "stems")
        for f in os.listdir(sd):
            if f.endswith(".npy") and not f.startswith("_"):
                p.stems[f[:-4]] = np.load(os.path.join(sd, f))
        p.original = np.load(os.path.join(sd, "_original.npy"))
        p.inst_sum = sum(p.stems[k] for k in p.instruments) if p.instruments else None
        p.B, p.centers = A.band_matrix(p.sr, 40)
        p.Bd, _ = A.band_matrix(p.sr, DISPLAY_BANDS, 30, 16000)
        z = np.load(os.path.join(root, "patches.npz"))
        for key in z.files:
            if key.endswith("__p"):
                sid = key[:-3]
                p.patches[sid] = (int(z[sid + "__lo"][0]), z[key])
        if os.path.exists(os.path.join(root, "display.npz")):
            z = np.load(os.path.join(root, "display.npz"))
            n = p.original.shape[1] // HOP + 1
            p.n_frames = n
            p.display = {"cols": n // DS, "fps": p.sr / HOP / DS, "raw": {}, "after": {}, "damage": {}}
            for key in z.files:
                kind, k = key.split("__", 1)
                p.display[kind][k] = z[key].astype(np.float32)
            p.display["ref_db"] = max(float(v.max()) for v in p.display["raw"].values())
        if os.path.exists(os.path.join(root, "features.npz")):
            z = np.load(os.path.join(root, "features.npz"))
            p.stem_feats, p.stem_env = {}, {}
            for k in p.instruments:
                p.stem_feats[k] = tuple(z[f"{k}__{i}"] for i in range(3))
                p.stem_env[k] = z[f"{k}__env"]
        p.level_floor = {}
        p._compute_grid()
        return p

    # ------------------------------------------------------------------ UI payload
    def spectro(self, k, kind):
        """uint8 image bytes (rows = DISPLAY_BANDS low->high, row-major) for the timeline."""
        ref = self.display.get("ref_db", 0.0)
        if kind == "damage":
            a = self.display["damage"].get(k)
            if a is None:
                a = np.zeros_like(self.display["raw"][k])
            return _to_u8(a, 0, 0.6).tobytes()
        src = self.display["after" if kind == "after" else "raw"]
        a = src.get(k, self.display["raw"][k])
        return _to_u8(a, ref - 85, ref).tobytes()

    def update_settings(self, values):
        with self.lock:
            st = self.meta["settings"]
            for key in ("auto_conf", "review_conf", "vocal_threshold", "min_severity"):
                if key in values:
                    st[key] = float(values[key])
            # re-sort untouched suggestions with the new thresholds (your own decisions are kept)
            for s in self.suggestions.values():
                if s["type"] == "repair":
                    s["recommended"] = s["conf"] >= st["auto_conf"]
                if s["type"] == "repair" and s["status"] in ("pending", "low") and not s.get("manual_donor"):
                    new = "pending" if s["conf"] >= st["review_conf"] else "low"
                    if new != s["status"]:
                        s["status"] = new
                        self._invalidate(s["stem"], s["id"])
            self._compute_grid()

    def rename_section(self, i, name):
        with self.lock:
            self.sections[int(i)]["name"] = str(name)[:40]

    # ------------------------------------------------------------------ quality tips
    def _beat_levels(self, y):
        """RMS level in dB of each beat of a (2, n) signal."""
        x = y.mean(0).astype(np.float64) ** 2
        idx = np.clip((np.asarray(self.beats) * HOP).astype(np.int64), 0, len(x) - 1)
        sums = np.add.reduceat(x, idx)[:-1]
        lens = np.maximum(np.diff(idx), 1)
        return 10 * np.log10(sums / lens + 1e-12)

    def tips(self):
        """Suggestions for keeping the fixed parts high quality and consistent with the rest of the song.
        Each tip carries an action the app can perform in one click (and undo)."""
        out = []
        if self.stage == "imported" or not len(self.VA):
            return out
        imported_stems = self.meta.get("separation") == "Imported stems"
        kept = self.keep_ranges()

        def covered(a, b):
            return any(r["start"] <= a + 0.05 and r["end"] >= b - 0.05 for r in kept)

        # 1. vocal-free parts: the original recording already is a perfect instrumental there
        if not imported_stems:
            free = []
            for sec in self.sections:
                a = int(np.searchsorted(self.beat_times, sec["start"]))
                b = int(np.searchsorted(self.beat_times, sec["end"]))
                va = np.asarray(self.VA[a:max(b, a + 1)])
                if len(va) and float(va.max()) < 0.04 and not covered(sec["start"], sec["end"]):
                    free.append(sec)
            if free:
                names = ", ".join(s["name"] for s in free)
                out.append({
                    "id": "keep_free", "title": f"Use the original recording in the parts without singing",
                    "detail": f"{names} {'has' if len(free) == 1 else 'have'} no singing, so the original recording already is "
                              f"a clean instrumental there, with no separation artifacts.",
                    "button": "Keep original there",
                    "action": {"kind": "keep", "ranges": [{"start": s["start"], "end": s["end"], "mode": "original"} for s in free]}})
        if self.stage != "detected":
            out.append({"id": "detect", "title": "Find the damage", "detail": "Step 3 finds the spots the vocals damaged and suggests repairs taken from the same instrument elsewhere in the song.",
                        "button": "Find damage", "action": {"kind": "detect"}})
            return out

        sugs = list(self.suggestions.values())
        rec = [x for x in sugs if x["type"] == "repair" and x["status"] == "pending" and x.get("recommended")]
        if rec:
            out.append({"id": "apply_rec", "title": f"Apply the {len(rec)} recommended repair{'s' if len(rec) != 1 else ''}",
                        "detail": "Each has a close match elsewhere in the song. Afterwards, look here again for anything that still sounds thinner.",
                        "button": "Apply recommended", "action": {"kind": "bulk", "action": "accept_recommended"}})

        # 2. tone match: what's still missing under the vocals, judged against the same part elsewhere
        lift_now = self.mixer.get("vocal_lift", {})
        for k in ([] if rec else self.instruments):     # judge what's left only once the repairs are in
            loss = self.remaining_loss(k)
            want = float(np.clip(round((loss + float(lift_now.get(k, 0))) * 2) / 2, 0, 6))
            threshold = 1.5 if lift_now.get(k) else 1.0
            if loss >= threshold and want - float(lift_now.get(k, 0)) >= 0.5:
                out.append({"id": f"lift_{k}", "title": f"Fill in the {k} under the vocals (+{want:.1f} dB)",
                            "detail": f"Next to the same part where nobody sings, the {k} is still about {loss:.1f} dB thinner in the "
                                      f"vocal range (250 Hz to 5 kHz) under the singing. This lifts only that range, only there.",
                            "button": f"Fill in +{want:.1f} dB", "action": {"kind": "lift", "stem": k, "db": want}})

        bleed = [x for x in sugs if x["type"] == "bleed" and x["status"] == "pending"]
        if bleed:
            stems_b = sorted({x["stem"] for x in bleed})
            out.append({"id": "bleed", "title": f"Remove leftover singing from the {', '.join(stems_b)}",
                        "detail": f"{len(bleed)} spot{'s' if len(bleed) != 1 else ''} still {'have' if len(bleed) != 1 else 'has'} a faint copy of the vocal.",
                        "button": "Review leftovers", "action": {"kind": "filter", "filter": "bleed"}})

        # an instrument that's mostly leftover singing in a section is better muted there
        mutes = self.mixer.get("mute_ranges", [])
        for si, sec in enumerate(self.sections):
            if not sec.get("vocal"):
                continue
            a, b = int(sec["start"] * self.sr), int(sec["end"] * self.sr)
            for k in self.instruments:
                if any(r.get("stem") in (None, k) and r["start"] <= sec["start"] + 0.05 and r["end"] >= sec["end"] - 0.05 for r in mutes):
                    continue
                bl = [x for x in sugs if x["type"] == "bleed" and x["stem"] == k and x["section"] == si]
                bars = max(1, sum(1 for (s0, L) in self.windows
                                  if sec["start"] <= self.beat_times[s0] < sec["end"]))
                if len(bl) < 0.6 * bars:
                    continue
                here = self._beat_levels(self.render_stem(k))
                bt = np.asarray(self.beat_times[:len(here)])
                inside = here[(bt >= sec["start"]) & (bt < sec["end"])]
                if len(inside) and np.median(inside) < np.median(here) - 10:
                    out.append({"id": f"mute_{k}_{si}", "title": f"Mute the {k} in {sec['name']}",
                                "detail": f"There the {k} is quiet and mostly leftover singing, so muting it removes the ghost vocal without losing much.",
                                "button": "Mute it there", "action": {"kind": "mute", "start": sec["start"], "end": sec["end"], "stem": k}})

        if self._applied_count() != self.meta.get("glitch_scan_applied", 0):
            out.append({"id": "rescan", "title": "Listen for clicks and dropouts again",
                        "detail": "You've changed repairs since the last check. A quick scan catches any new click or gap.",
                        "button": "Check now", "action": {"kind": "scan"}})
        glitches = [x for x in sugs if x["type"] == "glitch" and x["status"] == "pending"]
        if glitches:
            out.append({"id": "glitches", "title": f"{len(glitches)} click{'s' if len(glitches) != 1 else ''} or dropout{'s' if len(glitches) != 1 else ''} found",
                        "detail": "Short pops or gaps that don't belong. Each fix smooths over just a few milliseconds.",
                        "button": "Review them", "action": {"kind": "filter", "filter": "cleanup"}})

        hard = [x for x in sugs if x["type"] == "repair" and x["status"] in ("low", "nodonor")]
        if hard:
            out.append({"id": "hard", "title": f"{len(hard)} spot{'s have' if len(hard) != 1 else ' has'} no close match",
                        "detail": "Pick a source by hand, or leave them. A weak match can add notes that weren't there.",
                        "button": "Show them", "action": {"kind": "filter", "filter": "hard"}})

        mode = self.meta.get("sep_mode")
        dmg = np.mean([v["damage"] for v in self.stem_summary.values()]) if self.stem_summary else 0
        if mode in ("4stem_fast", "6stem") and dmg > 0.04 and not imported_stems:
            out.append({"id": "resplit", "title": "Split again with Best quality",
                        "detail": "Best quality takes less of the instruments with the vocals, so fewer repairs are needed.",
                        "button": "Split with Best quality", "action": {"kind": "resplit", "mode": "4stem"}})
        if not out:
            out.append({"id": "good", "title": "Nothing else to suggest",
                        "detail": "Compare Raw instrumental and Repaired at the top to hear the difference, then export.",
                        "button": None, "action": None})
        return out

    def overview(self):
        return {
            "meta": self.meta, "sections": self.sections, "beats": self.beat_times,
            "vocal_activity": [float(x) for x in self.VA], "stems": self.instruments,
            "colors": STEM_COLORS, "grid": self.grid, "stem_summary": self.stem_summary,
            "suggestions": list(self.suggestions.values()), "mixer": self.mixer,
            "levels": self.section_levels(), "can_undo": bool(self.history),
            "undo_label": self.history[-1]["label"] if self.history else None,
            "display": {"cols": self.display["cols"], "rows": DISPLAY_BANDS, "fps": self.display["fps"]},
            "stage": self.stage, "has_original_display": "original" in self.display.get("raw", {}),
            "tips": self.tips(),
            "user_decisions": sum(1 for x in self.suggestions.values() if x["status"] in ("accepted", "rejected")),
        }
