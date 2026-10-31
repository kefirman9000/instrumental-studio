"""A song project: stems, analysis, suggestions (repairs / bleed cleanup), levels, undo, save, export."""
import json
import os
import shutil
import threading
import time

import numpy as np

from . import analysis as A
from .audio import EPS, HOP, N_FFT, load_audio, save_audio, stft, istft, log
from .repair import (match_window, refine_lag, repair_window, bleed_window, smooth_window, ess_window,
                     MARGIN, XFADE, _window_weight)
from .audio import segment
from . import glitch as GL
from . import hits as HT
from . import verify as VF

DISPLAY_BANDS = 96
DS = 4                      # display columns = analysis frames / DS  (~21.5 columns per second)
STEM_ORDER = ["drums", "kick", "snare", "toms", "cymbals", "bass", "guitar", "piano", "other", "instrumental",
              "backing_vocals"]
NOT_REPAIRED = {"backing_vocals"}      # tracks that are kept or muted, never rebuilt from elsewhere
STEM_COLORS = {"drums": "#E8875B", "bass": "#5B8DEF", "guitar": "#4FB286", "piano": "#C46BD6",
               "other": "#D9B44A", "instrumental": "#5BC0D9", "vocals": "#E05C7A",
               "kick": "#F0A06E", "snare": "#E37B55", "toms": "#CC9A6A", "cymbals": "#F2C38A", "backing_vocals": "#B98ACF"}

DEFAULTS = {
    "auto_conf": 0.6,          # repairs at/above this match are marked "recommended" (never applied without asking)
    "review_conf": 0.3,        # below this a repair is marked "no good match"
    "min_severity": 0.02,      # ignore windows with less estimated loss than this
    "vocal_threshold": 0.08,   # beats with less vocal than this are treated as vocal-free
    "n_donors": 3,
    "beats_per_bar": 4,
    "guard_before": 0.3,       # "use the original song" stays this many seconds clear of singing starting...
    "guard_after": 0.8,        # ...and this long after it stops (breaths, reverb tails)
}


def _to_u8(x, lo=-80.0, hi=0.0):
    return np.clip((x - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)


class Project:
    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self._save_lock = threading.Lock()     # one save at a time
        self._saved = {}                       # what's on disk already: part -> signature
        self.meta = {"name": os.path.basename(root), "settings": dict(DEFAULTS), "created": time.time()}
        self.stems, self.sr = {}, 44100
        self.original = None
        self.suggestions = {}
        self.patches = {}          # id -> (lo, float16 patch[2, n])
        self.take_w = {}           # best-version edits: id -> (lo, float16 weight[n]) of what they replace
        self.history = []
        self.mixer = {"stem_gain": {}, "mute": {}, "solo": {}, "section_gain": {}, "keep": [], "vocal_lift": {}, "mute_ranges": [], "sides": [], "mud_cut": {}, "tone_eq": {}, "seams": [], "eq_ranges": []}
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
        for cache in ("_sounds_cache", "_dscale", "_guard", "_tail", "_eq_gain_cache", "_vo_cache", "_refs", "_drum_map", "_drum_samples", "_ref_dis", "_bar_chords", "_noise_prof"):
            self.__dict__.pop(cache, None)
        os.makedirs(os.path.join(self.root, "stems"), exist_ok=True)
        for k, v in self.stems.items():
            np.save(os.path.join(self.root, "stems", k + ".npy"), v)
        np.save(os.path.join(self.root, "stems", "_original.npy"), self.original)
        try:
            os.remove(os.path.join(self.root, "stems", "_recovered.npy"))     # belongs to the old vocal track
        except OSError:
            pass

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
        jobs = [(k, w) for k in inst_names if k not in NOT_REPAIRED for w in range(len(windows))]
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
        for key in ("quality_before", "quality", "verified", "grid_drift"):
            self.meta.pop(key, None)       # earlier scores and checks were for the earlier detection
        progress(0.95, "Double-checking repairs against the original")
        try:
            self.check_ids([i for i, x in self.suggestions.items() if x["type"] == "repair"])
        except Exception as e:
            log(f"check against the original skipped: {e}")
        progress(0.955, "Finding repeated hits and notes")
        for k in self.instruments:
            if k in self.PERCUSSIVE or self._tonal(k):
                try:
                    self._sounds(k)
                except Exception as e:
                    log(f"repeated hits skipped for {k}: {e}")
        progress(0.96, "Listening for clicks and dropouts")
        self.scan_glitches()
        progress(0.98, "Listening for leftover \"s\" sounds")
        try:
            self.find_ess()
        except Exception as e:          # an extra check must never stop Find damage
            log(f"ess check skipped: {e}")
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
        takes = []
        for sid, sug in self.suggestions.items():
            if sug["stem"] != k or not self._applied(sug) or sid not in self.patches:
                continue
            if sug["type"] in ("take", "alt", "bars"):
                takes.append(sid)
                continue
            lo, p = self.patches[sid]
            a, b = max(lo, 0), min(lo + p.shape[1], n)
            if b > a:
                delta[:, a:b] += p[:, a - lo:b - lo].astype(np.float32)
        # Replacing edits are laid down widest first, so the most specific wins where they overlap:
        # another model's split (alt) < a repeated bar < a best version of the whole part.
        takes.sort(key=lambda i: 3 if self.suggestions[i].get("alt_mode") == "imported" else
                   1.5 if self.suggestions[i].get("alt_mode") in ("notes", "pattern") else
                   {"alt": 0, "bars": 1, "take": 2}.get(self.suggestions[i]["type"], 0))
        for sid in takes:      # a best-version edit replaces the part outright, other edits included
            lo, p = self.patches[sid]
            _, w = self.take_w[sid]
            a, b = max(lo, 0), min(lo + p.shape[1], n)
            if b > a:
                ww = w[a - lo:b - lo].astype(np.float32)[None, :]
                delta[:, a:b] = delta[:, a:b] * (1 - ww) + p[:, a - lo:b - lo].astype(np.float32)
        kept = self.keep_mask(0, n)
        if kept is not None:          # kept parts are never repaired
            delta *= (1 - kept)[None, :]
        out += delta
        if (self.mixer.get("drum_edits") or {}).get(k):
            try:
                out = self._apply_drum_edits(k, out)
            except Exception as e:
                log(f"drum edits skipped for {k}: {e}")
        self._render_cache[key] = out
        gen = self.__dict__.setdefault("_render_gen", {})
        gen[k] = gen.get(k, 0) + 1
        return out

    def _stem_cached(self, name, k, extra, fn, lazy=False):
        """Per-instrument measurements are slow (whole-song analysis), and the suggestions list is rebuilt
        after every edit. Keep each result until that instrument is re-rendered or `extra` changes.
        lazy: don't make the caller wait; work it out in the background and return None for now."""
        self.render_stem(k)                                   # brings the render (and its generation) up to date
        key = (self.__dict__.get("_render_gen", {}).get(k, 0), extra)
        cache = self.__dict__.setdefault("_metric_cache", {})
        hit = cache.get((name, k))
        if hit is not None and hit[0] == key:
            return hit[1]
        if lazy:
            self._warm((name, k), lambda: cache.__setitem__((name, k), (key, fn())))
            return None
        v = fn()
        cache[(name, k)] = (key, v)
        return v

    def _warm(self, tag, job):
        """Run a slow measurement on the background worker (one at a time, so it never competes with
        itself), unless it's already queued. The window asks again while `warming` is true."""
        q = self.__dict__.setdefault("_warm_q", set())
        if tag in q:
            return
        pool = self.__dict__.get("_warm_pool")
        if pool is None:
            from concurrent.futures import ThreadPoolExecutor
            pool = self.__dict__["_warm_pool"] = ThreadPoolExecutor(max_workers=1, thread_name_prefix="warm")
        q.add(tag)

        def run():
            try:
                job()
            except Exception as e:
                log(f"background measurement {tag} failed: {e}")
            finally:
                q.discard(tag)
        pool.submit(run)

    @property
    def warming(self):
        return bool(self.__dict__.get("_warm_q"))

    # ------------------------------------------------------------------ kept parts
    def keep_ranges(self, mode=None):
        return [r for r in self.mixer.get("keep", []) if mode is None or r.get("mode") == mode]

    def keep_mask(self, a, b, mode=None, ramp=0.03):
        """0..1 per sample for [a, b): 1 inside kept parts, with short raised-cosine edges.
        Parts that use the original song stay clear of any singing (plus a safety margin), so a breath,
        a pickup note or a reverb tail never slips into the instrumental."""
        ranges = self.keep_ranges(mode)
        if not ranges:
            return None
        t = np.arange(a, b, dtype=np.float64) / self.sr
        m = np.zeros(b - a, np.float32)
        m_orig = np.zeros(b - a, np.float32)
        for r in ranges:
            up = np.clip((t - r["start"] + ramp / 2) / ramp, 0, 1)
            dn = np.clip((r["end"] + ramp / 2 - t) / ramp, 0, 1)
            w = (np.sin(0.5 * np.pi * np.minimum(up, dn)) ** 2).astype(np.float32)
            if r.get("mode") == "original":
                m_orig = np.maximum(m_orig, w)
            else:
                m = np.maximum(m, w)
        if m_orig.any():
            g = self.vocal_guard(a, b)
            if g is not None:
                m_orig *= (1 - g)
            self._smooth_seams(m_orig, a)
            m = np.maximum(m, m_orig)
        return m

    def _smooth_seams(self, m, a):
        """Lengthen the switch between the instrumental and the original recording where Smooth seams
        found it would be heard: a raised-cosine crossfade of the seam's own length, centred on it."""
        for sm in self.mixer.get("seams", []) or []:
            c = int(float(sm["t"]) * self.sr) - a
            h = int(float(sm.get("fade", 0.03)) * self.sr / 2)
            lo, hi = c - h, c + h
            if h < 2 or hi <= 0 or lo >= len(m):
                continue
            lo_c, hi_c = max(lo, 0), min(hi, len(m))
            m0 = float(m[lo_c]) if lo >= 0 else float(m[0])
            m1 = float(m[hi_c - 1]) if hi <= len(m) else float(m[-1])
            x = (np.arange(lo_c, hi_c) - lo) / max(hi - lo, 1)
            m[lo_c:hi_c] = (m0 + (m1 - m0) * np.sin(0.5 * np.pi * x) ** 2).astype(np.float32)

    def find_seams(self):
        """Every place the original recording takes over from the instrumental (or hands back), and how long
        a crossfade it needs so the switch can't be heard: longer where the instrumental sounds more
        different from the original there (level and tone, measured on the kept side where nobody sings)."""
        saved = self.mixer.get("seams", [])
        self.mixer["seams"] = []                        # measure the plain switches
        try:
            n = self.original.shape[1]
            m = self.keep_mask(0, n, "original")
        finally:
            self.mixer["seams"] = saved
        if m is None:
            return []
        step = max(1, self.sr // 100)
        mm = m[::step] > 0.5
        edges = np.flatnonzero(mm[1:] != mm[:-1]) + 1
        win = int(0.5 * self.sr)
        out = []
        for e in edges:
            t = int(e * step)
            kept_after = bool(mm[e])
            a, b = (t, min(n, t + win)) if kept_after else (max(0, t - win), t)
            if b - a < int(0.1 * self.sr):
                continue
            inst = sum(self.render_stem(k)[:, a:b] for k in self.instruments).mean(0)
            orig = self.original[:, a:b].mean(0)
            Ei = VF.band_energy(inst, self.B).sum(1) + 1e-9
            Eo = VF.band_energy(orig, self.B).sum(1) + 1e-9
            d_db = float(10 * np.log10(Ei.sum() / Eo.sum()))
            spec = float(np.sqrt(np.mean((10 * np.log10(Ei / Ei.sum()) - 10 * np.log10(Eo / Eo.sum())) ** 2)))
            fade = float(np.clip(0.03 + 0.06 * abs(d_db) + 0.04 * spec, 0.03, 0.6))
            out.append({"t": t / self.sr, "fade": round(fade, 3), "level_db": round(d_db, 1), "tone_db": round(spec, 1)})
        # neighbours: never let two fades overlap
        for i in range(len(out)):
            gap = min([abs(out[i]["t"] - out[j]["t"]) for j in range(len(out)) if j != i] or [9])
            out[i]["fade"] = round(min(out[i]["fade"], 0.9 * gap), 3)
        return out

    def vocal_guard(self, a, b):
        """1 near singing (with the safety margin before and after), 0 well away from it, smooth between.
        Measured on the vocal track itself every 10 ms, so it catches breaths and short notes too."""
        if "vocals" not in self.stems:
            return None
        if "_guard" not in self.__dict__:
            st = self.meta["settings"]
            v = self.stems["vocals"].mean(0)
            hop = max(1, self.sr // 100)
            m = len(v) // hop
            rms = np.sqrt((v[:m * hop].reshape(m, hop) ** 2).mean(1) + 1e-12)
            db = 20 * np.log10(rms + 1e-12)
            peak = np.percentile(db, 99)
            sung = db > max(peak - 40, -60)
            before = int(round(float(st.get("guard_before", 0.3)) * 100))
            after = int(round(float(st.get("guard_after", 0.8)) * 100))
            g = np.zeros(m, np.float32)
            idx = np.flatnonzero(sung)
            if len(idx):
                # grow every sung frame backwards by `before` and forwards by `after`
                d = np.zeros(m + 1, np.int32)
                np.add.at(d, np.clip(idx - before, 0, m), 1)
                np.add.at(d, np.clip(idx + after + 1, 0, m), -1)
                g = (np.cumsum(d)[:m] > 0).astype(np.float32)
                from scipy.ndimage import uniform_filter1d
                g = np.clip(uniform_filter1d(g, 5) * 1.25, 0, 1)      # ~50 ms fade, no hard edges
            self._guard = (hop, g)
        hop, g = self._guard
        t = np.arange(a, b) / hop
        return np.interp(t, np.arange(len(g)), g).astype(np.float32)

    def sides_mask(self, a, b, ramp=0.05):
        rs = self.mixer.get("sides", [])
        if not rs:
            return None
        t = np.arange(a, b, dtype=np.float64) / self.sr
        m = np.zeros(b - a, np.float32)
        for r in rs:
            up = np.clip((t - r["start"] + ramp / 2) / ramp, 0, 1)
            dn = np.clip((r["end"] + ramp / 2 - t) / ramp, 0, 1)
            m = np.maximum(m, float(r.get("amount", 1.0)) * np.minimum(up, dn).astype(np.float32))
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
            auto = r.get("auto") or r.get("acappella")
            rp = float(r.get("fade", 0.15 if auto else ramp))                 # the app's mutes fade in and out
            amt = float(r.get("amount", 0.82 if auto else 1.0))               # ...and only turn down (-15 dB)
            up = np.clip((t - r["start"] + rp / 2) / rp, 0, 1)
            dn = np.clip((r["end"] + rp / 2 - t) / rp, 0, 1)
            w = np.sin(0.5 * np.pi * np.minimum(up, dn)) ** 2
            m = np.maximum(m, (amt * w).astype(np.float32))
        return m

    def vocal_only_runs(self, aggressive=False):
        """Stretches where only the singer is heard (a cappella): whatever the instrument tracks hold there is
        leftover singing, so it's safe to silence all of them. [(start_s, end_s)].
        Careful: the split vocal holds nearly all of the original there, the instruments are at least 20 dB
        under it (or 12 dB and shaped like the voice), for 2 beats or more. Aggressive: 8 dB (or 4 dB and shaped
        like the voice), from a single beat."""
        key = ("vo", bool(aggressive))
        cache = self.__dict__.setdefault("_vo_cache", {})
        if key in cache:
            return cache[key]
        if not len(self.beats) or not self.instruments or self.meta.get("separation") == "Imported stems":
            return []
        bt = (np.asarray(self.beats, int) * HOP).clip(0, self.original.shape[1])
        cum = lambda y: np.r_[0, np.cumsum(np.asarray(y, np.float64).mean(0) ** 2)]
        Ci, Cv, Co = cum(self.inst_sum), cum(self.stems["vocals"]), cum(self.original)
        per = lambda C: np.diff(C[bt]) + 1e-12
        Ei, Ev, Eo = per(Ci), per(Cv), per(Co)
        loud = Ev > 1e-3 * float(np.median(Eo[Eo > 1e-10])) if np.any(Eo > 1e-10) else np.zeros_like(Ev, bool)
        Bi = np.log(VF.band_energy(self.inst_sum.mean(0), self.B) + 1e-10)
        Bv = np.log(VF.band_energy(self.stems["vocals"].mean(0), self.B) + 1e-10)
        fb = np.asarray(self.beats, int).clip(0, Bi.shape[1] - 1)
        voice_like = np.zeros(len(Ei), bool)
        for j in range(len(Ei)):
            a, b = fb[j], max(fb[j] + 1, fb[j + 1])
            x, y = Bi[:, a:b].mean(1), Bv[:, a:b].mean(1)
            if x.std() > 1e-3 and y.std() > 1e-3:
                voice_like[j] = np.corrcoef(x, y)[0, 1] > 0.75
        ratio = 10 * np.log10(Ei / Ev)
        share = Ev / Eo
        va = np.asarray(self.VA[:len(Ei)], float) if len(self.VA) else np.ones(len(Ei))
        va = np.pad(va, (0, len(Ei) - len(va)))
        lim, lim_v, min_share, min_beats = (-8, -4, 0.5, 2) if aggressive else (-20, -12, 0.7, 4)
        ok = loud & (va > 0.3) & (share > min_share) & ((ratio < lim) | ((ratio < lim_v) & voice_like))
        # the singer pausing inside an a cappella part (instruments silent there too) joins the stretch
        ref_lvl = float(np.median(Eo[Eo > 1e-10])) if np.any(Eo > 1e-10) else 1.0
        silent = 10 * np.log10(Ei / ref_lvl) < -35
        bt_s = np.asarray(self.beat_times, float)
        idx, j = [], 0
        while j < len(ok):
            if ok[j]:
                e = j
                while e + 1 < len(ok) and ok[e + 1]:
                    e += 1
                idx.append([j, e])
                j = e + 1
            else:
                j += 1
        merged = []
        for r in idx:
            if merged:
                g0, g1 = merged[-1][1] + 1, r[0]               # beats in the gap
                if g1 > g0 and bt_s[min(g1, len(bt_s) - 1)] - bt_s[g0] <= 1.0 and silent[g0:g1].all():
                    merged[-1][1] = r[1]
                    continue
            merged.append(list(r))
        runs = [(float(bt_s[j]), float(bt_s[min(e + 1, len(bt_s) - 1)])) for j, e in merged if e - j + 1 >= min_beats]
        runs = [(a_, b_) for a_, b_ in runs if b_ - a_ >= 2.0]                 # short cuts do more harm than good
        cache[key] = runs
        return runs

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
                ws = self.sides_mask(a, b)
                if ws is not None:     # the original's stereo sides hold the instruments, not the lead vocal
                    mid, side = (out[0] + out[1]) / 2, (out[0] - out[1]) / 2
                    oside = (self.original[0, a:b] - self.original[1, a:b]) / 2
                    side = side * (1 - ws) + oside * ws
                    out = np.vstack([mid + side, mid - side]).astype(np.float32)
                mo = self.keep_mask(a, b, "original")
                if mo is not None:
                    out = out * (1 - mo)[None, :] + self.original[:, a:b] * mo[None, :]
                mall = self.mute_mask(a, b, only_all=True)
                if mall is not None:
                    out = out * (1 - mall)[None, :]
            return out
        return out[:, region[0]:region[1]] if region else out

    LIFT_BAND = (250.0, 5000.0)   # where vocals cover instruments, so where separation thins them
    MUD_BAND = (150.0, 500.0)     # where leftover voice and stacked repairs build up as mud

    def shaped_stem(self, k, a=0, b=None, version="repaired"):
        """Rendered stem for [a, b) including the full tone match, the 'fill under the vocals' lift and the
        'clear the mud under the vocals' cut, and frequency cuts, if set. All act only under the singing.
        The whole-song result is kept until the instrument or those settings change."""
        if version == "repaired" and a == 0 and (b is None or b >= self.stems[k].shape[1]):
            m = self.mixer
            extra = json.dumps([m.get("tone_eq", {}).get(k), m.get("vocal_lift", {}).get(k), m.get("mud_cut", {}).get(k),
                                [r for r in (m.get("eq_ranges") or []) if r.get("stem") in (None, "", k)],
                                m.get("keep", []), m.get("seams", [])], sort_keys=True)
            return self._stem_cached("shaped", k, extra, lambda: self._shaped(k, 0, None, version))
        return self._shaped(k, a, b, version)

    def _shaped(self, k, a=0, b=None, version="repaired"):
        rep_ = version == "repaired"
        y = self._toned(k) if rep_ and self.mixer.get("tone_eq", {}).get(k) else self.render_stem(k, version)
        b = y.shape[1] if b is None else b
        seg = y[:, a:b]
        lift = float(self.mixer.get("vocal_lift", {}).get(k, 0.0)) if rep_ else 0.0
        cut = float(self.mixer.get("mud_cut", {}).get(k, 0.0)) if rep_ else 0.0
        if not lift and not cut:
            return self._apply_eq_ranges(k, seg, a, b) if rep_ else seg
        from scipy import signal as sig
        pad = int(0.2 * self.sr)
        lo, hi = max(0, a - pad), min(y.shape[1], b + pad)
        vc = self._vocal_curve(a, b)
        kept = self.keep_mask(a, b)
        if kept is not None:
            vc = vc * (1 - kept)
        out = seg
        for db, rng in ((lift, self.LIFT_BAND), (cut, self.MUD_BAND)):
            if not db:
                continue
            sos = sig.butter(2, rng, "bandpass", fs=self.sr, output="sos")
            band = sig.sosfiltfilt(sos, y[:, lo:hi], axis=1)[:, a - lo:a - lo + (b - a)].astype(np.float32)
            out = out + band * ((10 ** (db / 20) - 1) * vc)[None, :]
        return self._apply_eq_ranges(k, out, a, b)

    # ------------------------------------------------------------------ where leftover singing remains
    MAP_RATE = 4             # values per second

    def _leftover_curve(self, k):
        """0..1, MAP_RATE per second: how much instrument k still moves like the singer there (as it sounds
        now: repairs, cuts and mutes included)."""
        n = self.stems[k].shape[1]
        y = self.shaped_stem(k) * self._gain_curve(k, 0, n)
        Es = VF.band_energy(np.asarray(y, np.float32).mean(0), self.B)
        Ev = VF.band_energy(self.stems["vocals"].mean(0), self.B)
        nf = min(Es.shape[1], Ev.shape[1])
        vb = (np.asarray(self.centers) >= 250) & (np.asarray(self.centers) <= 5000)
        X, Y = np.log(Es[vb, :nf] + 1e-10), np.log(Ev[vb, :nf] + 1e-10)
        nb = X.shape[0]
        # per-frame sums, then running sums: the correlation over any window of frames in O(1)
        cs = lambda a: np.r_[0.0, np.cumsum(a)]
        Sx, Sy, Sxx, Syy, Sxy = (cs(X.sum(0)), cs(Y.sum(0)), cs((X * X).sum(0)), cs((Y * Y).sum(0)), cs((X * Y).sum(0)))
        share_s = cs(Es[vb, :nf].sum(0))
        tot_s = cs(Es[:, :nf].sum(0))
        voc = cs(self._vocal_curve(0, nf * HOP)[::HOP][:nf])
        fps = self.sr / HOP
        win, hop = int(0.75 * fps), max(1, int(fps / self.MAP_RATE))
        out = []
        for c in range(0, nf, hop):
            a, b = max(0, c - win // 2), min(nf, c + win // 2)
            m = (b - a) * nb
            if b - a < 8 or (voc[b] - voc[a]) / (b - a) < 0.2:
                out.append(0.0)
                continue
            sx, sy = Sx[b] - Sx[a], Sy[b] - Sy[a]
            vx = (Sxx[b] - Sxx[a]) - sx * sx / m
            vy = (Syy[b] - Syy[a]) - sy * sy / m
            cv = (Sxy[b] - Sxy[a]) - sx * sy / m
            if vx <= 1e-9 or vy <= 1e-9:
                out.append(0.0)
                continue
            r = cv / np.sqrt(vx * vy)
            share = (share_s[b] - share_s[a]) / ((tot_s[b] - tot_s[a]) + 1e-12)
            out.append(round(float(np.clip((r - 0.35) / 0.4, 0, 1) * np.clip(share * 1.5, 0, 1)), 2))
        return out

    def leftover_map(self, lazy=True):
        if self.stage != "detected":
            return {}
        out = {}
        for k in self.instruments:
            if k in NOT_REPAIRED:
                continue
            extra = (json.dumps([self.mixer.get(x) for x in ("eq_ranges", "mute_ranges", "tone_eq", "vocal_lift", "mud_cut",
                                                             "keep", "section_gain", "stem_gain")], sort_keys=True),
                     len(self.suggestions))
            out[k] = self._stem_cached("leftmap", k, extra, lambda k=k: self._leftover_curve(k), lazy)
        return out

    def leftover_runs(self, k, curve=None, thr=0.5, min_s=0.5):
        """Stretches where instrument k still has clear leftover singing: [(start, end)] in seconds."""
        c = curve if curve is not None else self._leftover_curve(k)
        runs, start = [], None
        for i, v in enumerate(list(c) + [0.0]):
            t = i / self.MAP_RATE
            if v >= thr and start is None:
                start = t
            elif v < thr and start is not None:
                if t - start >= min_s:
                    runs.append((max(0.0, start - 0.25), t + 0.25))
                start = None
        return runs

    # ------------------------------------------------------------------ second vocal-removal pass on an instrument
    def add_deep_leftovers(self, k, found):
        """`found` is what a vocal model found in instrument k on a second pass. Offer to take it out, per
        section, only in bars where someone is singing AND what was found moves like the actual singing
        (so a melody line the model mistook for a voice is left alone). Returns the new review ids."""
        with self.lock:
            n = self.stems[k].shape[1]
            found = found[:, :n]
            vb = (np.asarray(self.centers) >= 250) & (np.asarray(self.centers) <= 5000)
            w = np.zeros(n, np.float32)
            for wi, (s0, L) in enumerate(self.windows):
                a, b = self._window_samples(s0, L)
                b = min(b, n)
                prev = self.windows[wi - 1] if wi else None
                sung_here = float(np.mean(self.VA[s0:s0 + L]) if L else 0) >= 0.2
                sung_before = bool(prev) and float(np.mean(self.VA[prev[0]:prev[0] + prev[1]]) if prev[1] else 0) >= 0.2
                if b - a < self.sr // 4 or not (sung_here or sung_before):   # the bar after a line holds its reverb
                    continue
                Ef = VF.band_energy(found[:, a:b].mean(0), self.B)[vb]
                Ev = VF.band_energy(self.stems["vocals"][:, a:b].mean(0), self.B)[vb]
                Es = VF.band_energy(self.stems[k][:, a:b].mean(0), self.B)[vb]
                if Ef.sum() < 1e-3 * (Es.sum() + 1e-12):
                    continue                                   # nothing worth taking out
                x, y = np.log(Ef + 1e-10).ravel(), np.log(Ev + 1e-10).ravel()
                if x.std() < 1e-3 or y.std() < 1e-3 or np.corrcoef(x, y)[0, 1] < 0.5:
                    continue                                   # doesn't move like the singer
                w[a:b] = 1
            if not w.any():
                return []
            from scipy.ndimage import uniform_filter1d
            w = uniform_filter1d(w, int(0.03 * self.sr)) * np.maximum(np.clip(self._vocal_curve(0, n) * 1.5, 0, 1),
                                                                       self._vocal_tail(0, n))
            ids = []
            for si, sec in enumerate(self.sections):
                a, b = int(sec["start"] * self.sr), min(n, int(sec["end"] * self.sr))
                if b <= a or not w[a:b].any():
                    continue
                patch = -(found[:, a:b] * w[None, a:b])
                rem = float((patch ** 2).sum())
                tot = float((self.stems[k][:, a:b] ** 2).sum()) + 1e-12
                if rem < 1e-4 * tot:
                    continue
                rid = f"d_{k}_{si}"
                self.suggestions[rid] = {
                    "id": rid, "type": "bleed", "stem": k, "status": "pending", "start": sec["start"], "end": sec["end"],
                    "section": si, "conf": 0.9, "severity": float(min(1.0, rem / tot * 10)),
                    "removed_db": float(10 * np.log10(rem / tot + 1e-12)), "deep": True, "donors": [],
                    "note": f"Removes leftover singing a second vocal-removal pass found in the {k} (only where it moves like the singer)"}
                self.patches[rid] = (a, patch.astype(np.float16))
                ids.append(rid)
            return ids

    # ------------------------------------------------------------------ vinyl crackle, after splitting
    def declick_stems(self, strength="normal", progress=lambda f, m: None):
        """Remove vinyl clicks and crackle from each instrument track directly (no new split, so repairs and
        decisions stay). One review item per instrument and section, under Clicks and smoothing."""
        from . import vinyl as VN
        with self.lock:
            ids = []
            ks = [k for k in self.instruments]
            for i, k in enumerate(ks):
                x = self.stems[k]
                y, n = VN.declick(x, self.sr, strength,
                                  progress=lambda f, i=i: progress((i + f) / max(1, len(ks)), f"Removing crackle from the {k}"))
                if not n:
                    continue
                diff = (y - x).astype(np.float32)
                changed = np.flatnonzero(np.abs(diff).max(0) > 1e-7)
                for si, sec in enumerate(self.sections):
                    a, b = int(sec["start"] * self.sr), min(x.shape[1], int(sec["end"] * self.sr))
                    c = changed[(changed >= a) & (changed < b)]
                    if not len(c):
                        continue
                    lo, hi = int(c[0]), int(c[-1]) + 1
                    clicks = int(np.sum(np.diff(c) > int(0.002 * self.sr)) + 1)
                    rid = f"vc_{k}_{si}"
                    self.suggestions[rid] = {
                        "id": rid, "type": "glitch", "stem": k, "status": "pending", "start": lo / self.sr,
                        "end": hi / self.sr, "section": si, "conf": 1.0, "severity": 0.0, "donors": [], "vinyl": True,
                        "note": f"Removes {clicks} vinyl click{'s' if clicks != 1 else ''} and crackle from the {k} ({strength})"}
                    self.patches[rid] = (lo, diff[:, lo:hi].astype(np.float16))
                    ids.append(rid)
            return ids

    # ------------------------------------------------------------------ recover what the split put in the vocals
    def _recover_target(self):
        for k in ("other", "piano", "guitar"):
            if k in self.instruments:
                return k
        return next((k for k in self.instruments if k not in NOT_REPAIRED and k not in self.PERCUSSIVE), None)

    def add_recovered(self, rec):
        """`rec`: the non-vocal sound a model found inside the vocal track (synths, effects, guitar licks the split
        filed as "vocals"). Offer it back, per section, in bars where it doesn't move like the singer. Returns ids."""
        with self.lock:
            k = self._recover_target()
            if k is None:
                return []
            n = self.stems[k].shape[1]
            rec = np.asarray(rec, np.float32)[:, :n]
            # only what stands clear of the noise floor comes back: per frequency, anything within 8 dB of its
            # quiet level (20th percentile over the song) is separation hiss, not a part
            gated = np.empty_like(rec)
            for c in range(rec.shape[0]):
                R = stft(rec[c])
                mag = np.abs(R)
                floor = np.percentile(mag, 20, axis=1, keepdims=True) * 10 ** (8 / 20)
                keep = np.clip((mag - floor) / (floor + 1e-9), 0, 1)
                gated[c] = istft(R * keep, rec.shape[1])
            rec = gated
            vb = (np.asarray(self.centers) >= 250) & (np.asarray(self.centers) <= 5000)
            w = np.zeros(n, np.float32)
            inst_level = float((self.inst_sum[:, :n] ** 2).mean()) + 1e-12
            for wi, (s0, L) in enumerate(self.windows):
                a, b = self._window_samples(s0, L)
                b = min(b, n)
                if b - a < self.sr // 4:
                    continue
                seg = rec[:, a:b]
                if float((seg ** 2).mean()) < 3e-3 * inst_level:
                    continue                                       # too quiet to be a real part (-25 dB under the music)
                Er = VF.band_energy(seg.mean(0), self.B)[vb]
                Ev = VF.band_energy(self.stems["vocals"][:, a:b].mean(0), self.B)[vb]
                x, y = np.log(Er + 1e-10).ravel(), np.log(Ev + 1e-10).ravel()
                if x.std() > 1e-3 and y.std() > 1e-3 and np.corrcoef(x, y)[0, 1] > 0.6:
                    continue                                       # moves like the voice: not an instrument
                w[a:b] = 1
            if not w.any():
                return []
            from scipy.ndimage import uniform_filter1d
            w = uniform_filter1d(w, int(0.03 * self.sr))
            ids = []
            for si, sec in enumerate(self.sections):
                a, b = int(sec["start"] * self.sr), min(n, int(sec["end"] * self.sr))
                if b <= a or not w[a:b].any():
                    continue
                patch = rec[:, a:b] * w[None, a:b]
                if float((patch ** 2).mean()) < 1e-4 * inst_level:
                    continue
                rid = f"rc_{si}"
                if rid in self.suggestions and self.suggestions[rid]["status"] != "pending":
                    continue                                       # already decided: leave it
                self.suggestions[rid] = {
                    "id": rid, "type": "rebuild", "stem": k, "status": "pending", "start": sec["start"], "end": sec["end"],
                    "section": si, "conf": 0.9, "severity": 0.0, "donors": [], "recover": True,
                    "note": "Brings back sounds the split put in the vocal track (synths, effects, small details), only "
                            "where they don't move like the singer"}
                self.patches[rid] = (a, patch.astype(np.float16))
                ids.append(rid)
            self.check_ids(ids)
            return ids

    # ------------------------------------------------------------------ your own clean tracks and samples
    def _ref_path(self, k):
        return os.path.join(self.root, "stems", f"_ref_{k}.npy")

    def ref_track(self, k):
        """Your clean track of instrument k, lined up with the song and level-matched, or None."""
        if k not in (self.meta.get("refs") or {}):
            return None
        cache = self.__dict__.setdefault("_refs", {})
        if k not in cache:
            try:
                cache[k] = np.load(self._ref_path(k))          # read whole: a mapped file can't be replaced on Windows
            except (OSError, ValueError):
                return None
        return cache[k]

    def _align_track(self, audio, k):
        """(offset in samples where the file starts in the song, match 0..1) against instrument k: attack
        envelopes cross-correlated."""
        from scipy import signal as sig
        sr = self.sr
        ea = VF.onset_env(np.asarray(audio, np.float32).mean(0), sr)
        eo = VF.onset_env(self.stems[k].mean(0), sr)
        if len(ea) < 200 or len(eo) < 200:
            raise ValueError("That file is too short to line up with the song.")
        xc = sig.fftconvolve(eo, ea[::-1], mode="full")
        lag_ms = int(np.argmax(xc)) - (len(ea) - 1)
        o0, o1 = max(0, lag_ms), min(len(eo), lag_ms + len(ea))
        sa = ea[o0 - lag_ms:o1 - lag_ms]
        score = float(xc.max()) / (np.linalg.norm(eo[o0:o1]) * np.linalg.norm(sa) + 1e-9)
        return int(round(lag_ms * sr / 1000)), float(score)

    def identify_track(self, audio):
        """Which instrument a clean full-length track is: the one it lines up with best, weighted by how alike
        their sound (overall band shape) is. Returns (instrument, match) or (None, 0)."""
        audio = np.asarray(audio, np.float32)
        if audio.shape[0] == 1:
            audio = np.repeat(audio, 2, axis=0)
        shape = lambda y: (lambda e: e - e.mean())(np.log(VF.band_energy(np.asarray(y, np.float32).mean(0)[::4], self.B,
                                                                         max_frames=512).mean(1) + 1e-9))
        sa = shape(audio)
        best = (None, 0.0)
        for k in self.instruments:
            try:
                off, sc = self._align_track(audio, k)
            except ValueError:
                continue
            sk = shape(self.stems[k])
            cos = float(sa @ sk / (np.linalg.norm(sa) * np.linalg.norm(sk) + 1e-9))
            val = sc * (0.5 + 0.5 * max(cos, 0))
            if val > best[1]:
                best = (k, val)
        return best

    def add_reference_loop(self, audio, name, use="vocals", k=None):
        """A clean sample or loop used in the song (half a bar or longer): find every place it plays, on the
        instrument it sounds like (or `k`), and offer it there, stretched to each spot's exact length, lined up and
        level-matched. use="vocals": only where the singing damaged it. Returns (ids, what in words)."""
        with self.lock:
            sr = self.sr
            smp = np.asarray(audio, np.float32)
            if smp.shape[0] == 1:
                smp = np.repeat(smp, 2, axis=0)
            on = HT.onsets(smp.mean(0), sr)
            if len(on) and on[0] < 0.25 * sr:
                smp = smp[:, max(0, int(on[0]) - int(0.003 * sr)):]          # trim leading silence
            full = max(L for _, L in self.windows)
            bar_s = float(np.median([self._window_samples(s0, L)[1] - self._window_samples(s0, L)[0]
                                     for s0, L in self.windows if L == full]))
            nbars = smp.shape[1] / bar_s
            cands = [m for m in (0.5, 1, 2, 4, 8) if abs(nbars / m - 1) < 0.12]
            if not cands:
                raise ValueError(f"\"{name}\" is {nbars:.2f} bars long at this song's tempo; a loop needs to be half a "
                                 f"bar, 1, 2, 4 or 8 bars.")
            m = cands[0]
            nb = int(round(m * full))
            # its quarter-beat pattern, from the file stretched to an even grid
            E = np.log(VF.band_energy(smp.mean(0), self.B) + 1e-9)
            idx = np.linspace(0, E.shape[1], nb * 4 + 1).astype(int)
            Qs = np.stack([E[:, idx[i]:max(idx[i] + 1, idx[i + 1])].mean(1) for i in range(nb * 4)])
            Qs = Qs - Qs.mean(0, keepdims=True)
            vs = Qs.ravel() / (np.linalg.norm(Qs) + 1e-9)
            shape_s = E.mean(1) - E.mean()
            bar_starts = [s0 for s0, L in self.windows if L == full]
            if m == 0.5:
                bar_starts = sorted(bar_starts + [b + nb for b in bar_starts])
            best = None
            for kk in ([k] if k else self.instruments):
                Q = self._quarter_beat_feats(self.stems[kk])
                Ek = Q.mean(0) - Q.mean()
                cos_shape = float(shape_s @ Ek / (np.linalg.norm(shape_s) * np.linalg.norm(Ek) + 1e-9))
                sims = []
                for b0 in bar_starts:
                    if (b0 + nb) * 4 > len(Q):
                        continue
                    X = Q[b0 * 4:(b0 + nb) * 4]
                    X = X - X.mean(0, keepdims=True)
                    sims.append((float(X.ravel() @ vs / (np.linalg.norm(X) + 1e-9)), b0))
                if not sims:
                    continue
                top = max(x for x, _ in sims)
                score = top * (0.5 + 0.5 * max(cos_shape, 0))
                if best is None or score > best[0]:
                    best = (score, kk, sims, top)
            if best is None or best[3] < 0.6:
                raise ValueError(f"Couldn't find where \"{name}\" plays in this song.")
            _, k, sims, top = best
            hits = sorted(b0 for x, b0 in sims if x >= max(0.6, top - 0.15))
            raw, n = self.stems[k], self.stems[k].shape[1]
            if use == "vocals":
                from scipy.ndimage import maximum_filter1d, uniform_filter1d
                vw = np.clip(self._vocal_curve(0, n) * 4, 0, 1)
                vw = uniform_filter1d(maximum_filter1d(vw, int(0.25 * sr)), int(0.04 * sr)).astype(np.float32)
            by_sec = {}
            for b0 in hits:
                a, b = int(self.beats[b0] * HOP), int(self.beats[b0 + nb] * HOP)
                ratio = (b - a) / smp.shape[1]
                if abs(ratio - 1) > 0.08 or b > n:
                    continue                                  # this spot is too far off the file's speed
                if use == "vocals" and float(vw[a:b].max()) < 0.05:
                    continue                                  # clean already
                xi = np.linspace(0, smp.shape[1] - 1, b - a)
                placed = np.stack([np.interp(xi, np.arange(smp.shape[1]), smp[c]) for c in range(2)]).astype(np.float32)
                sh = HT.align(placed.mean(0)[:sr // 2], raw[:, a:a + sr // 2].mean(0), int(0.01 * sr))
                a2 = int(np.clip(a - sh, 0, n - placed.shape[1]))
                vc = self._vocal_curve(a2, a2 + placed.shape[1]) < 0.1
                mk = vc if vc.sum() > sr // 8 else np.ones(placed.shape[1], bool)
                g = float(np.clip(np.sqrt(float((raw[:, a2:a2 + placed.shape[1]][:, mk] ** 2).mean()) /
                                          (float((placed[:, mk] ** 2).mean()) + 1e-12)), 0.1, 10))
                by_sec.setdefault(self._section_of(a2 / sr), []).append((a2, placed * g))
            ids = []
            for si, items in by_sec.items():
                lo = max(0, min(x[0] for x in items) - XFADE)
                hi = min(n, max(x[0] + x[1].shape[1] for x in items) + XFADE)
                tgt = raw[:, lo:hi].copy()
                W = np.zeros(hi - lo, np.float32)
                ramp = int(0.01 * sr)
                for a2, y in items:
                    L = y.shape[1]
                    w = np.ones(L, np.float32)
                    w[:ramp] = np.linspace(0, 1, ramp)
                    w[-ramp:] = np.linspace(1, 0, ramp)
                    tgt[:, a2 - lo:a2 - lo + L] = y
                    W[a2 - lo:a2 - lo + L] = np.maximum(W[a2 - lo:a2 - lo + L], w)
                if use == "vocals":
                    W *= vw[lo:hi]
                rid = f"loop_{k}_{si}_{__import__('zlib').crc32(name.encode()) % 10000}"
                sec = self.sections[si]
                self.suggestions[rid] = {
                    "id": rid, "type": "alt", "stem": k, "status": "pending", "start": lo / sr, "end": hi / sr,
                    "section": si, "conf": round(float(top), 2), "severity": 0.0, "strength": 1.0, "donors": [],
                    "alt_mode": "imported", "blend": 0.0, "reference": name, "use": use,
                    "note": f"Places your \"{name}\" ({'half a bar' if m == 0.5 else f'{m:g} bar' + ('s' if m > 1 else '')}) "
                            f"where it plays in {sec['name']}, {len(items)} time{'s' if len(items) != 1 else ''}, lined up and "
                            f"level-matched" + (" (only where the singing damaged it)" if use == "vocals" else "")}
                self.patches[rid] = (lo, (W[None, :] * (tgt - raw[:, lo:hi])).astype(np.float16))
                self.take_w[rid] = (lo, W.astype(np.float16))
                ids.append(rid)
            what = (f"a {'half-bar' if m == 0.5 else f'{m:g}-bar'} loop on the {k}, found in {len(hits)} "
                    f"place{'s' if len(hits) != 1 else ''} ({top:.0%} match)")
            return ids, what

    def remove_reference(self, k):
        """Stop using your clean track of instrument k: its parts are taken out (Undo brings them back) and
        fixes stop being checked against it."""
        with self.lock:
            ids = [i for i, x in self.suggestions.items() if x.get("alt_mode") == "imported" and x["stem"] == k
                   and i.startswith(f"ref_{k}_") and x["status"] != "rejected"]
            if ids:
                self.set_status(ids, "rejected", f"Stopped using your clean {k} track")
            (self.meta.get("refs") or {}).pop(k, None)
            self.__dict__.get("_refs", {}).pop(k, None)
            try:
                os.remove(self._ref_path(k))
            except OSError:
                pass
            return ids

    REF_MODES = ("damaged", "stem", "mix", "split")      # per section: where singing damaged it, all, half, none

    def add_reference_track(self, audio, k, name, use="vocals"):
        """A clean recording of one instrument from this song (an official stem, a session file...): line it up
        with the song, match its level, keep it with the song, and use it for that instrument, section by
        section. use="vocals": only where the singing damaged the split (the rest of the split stays);
        use="all": the whole track. From then on every fix on that instrument is also checked against it.
        It gets its own lane, where you choose per section, paint where it's used, set its level and timing.
        Returns ids (waiting in Review)."""
        with self.lock:
            if k not in self.stems:
                raise ValueError(f"No {k} track in this song.")
            sr, n = self.sr, self.original.shape[1]
            audio = np.asarray(audio, np.float32)
            if audio.shape[0] == 1:
                audio = np.repeat(audio, 2, axis=0)
            off, score = self._align_track(audio, k)
            if score < 0.3:
                raise ValueError(f"Couldn't line \"{name}\" up with the {k} in this song (match {score:.0%}). Is it from "
                                 f"this recording, at the same speed?")
            ref = np.zeros((2, n), np.float32)
            s0, s1 = max(0, off), min(n, off + audio.shape[1])
            ref[:, s0:s1] = audio[:, s0 - off:s1 - off]
            a = s0 + int(0.5 * sr)                     # refine to the sample (within +-5 ms) on the first strong part
            if a + sr < s1:
                sh = HT.align(self.stems[k][:, a:a + sr].mean(0), ref[:, a:a + sr].mean(0), int(0.005 * sr))
                ref = np.roll(ref, -sh, axis=1)
            drift = 0.0
            try:
                ref, drift = self._fix_drift(ref, k)
            except Exception as e:
                log(f"drift check skipped: {e}")
            raw = self.stems[k]
            vq = self._vocal_curve(0, n) < 0.1          # one overall level, matched where nobody sings
            mq = vq if vq.sum() > sr else np.ones(n, bool)
            g_all = float(np.clip(np.sqrt(float((raw[:, mq] ** 2).mean()) / (float((ref[:, mq] ** 2).mean()) + 1e-12)), 0.1, 10))
            os.makedirs(os.path.join(self.root, "stems"), exist_ok=True)
            self.__dict__.setdefault("_refs", {}).pop(k, None)     # let go of the old copy before replacing it
            import gc
            gc.collect()
            tmp = self._ref_path(k) + ".new.npy"
            np.save(tmp, (ref * g_all).astype(np.float16))
            for _ in range(5):
                try:
                    os.replace(tmp, self._ref_path(k))
                    break
                except OSError:
                    time.sleep(0.3)
            else:
                raise RuntimeError("Couldn't save the clean track (the old copy is in use). Restart the app and add it again.")
            self.display.get("raw", {}).pop(f"ref:{k}", None)
            for rid in [i for i, x in self.suggestions.items() if i.startswith(f"ref_{k}_")]:
                self.suggestions.pop(rid, None)
                self.patches.pop(rid, None)
                self.take_w.pop(rid, None)
            mode = "damaged" if use == "vocals" else "stem"
            self.meta.setdefault("refs", {})[k] = {
                "name": name, "match": round(float(score), 2), "use": use, "gain_db": 0.0, "nudge_ms": 0.0,
                "mute": False, "sections": {str(si): mode for si in range(len(self.sections))}, "paint": [],
                "tone_match": True, "drift": round(drift, 2)}
            return self._rebuild_ref(k, "pending")

    def _ref_weight(self, k, cfg):
        """0..1 per sample: how much of instrument k comes from your clean track, from the per-section choices
        and what you painted on its lane."""
        sr, n = self.sr, self.original.shape[1]
        W = np.zeros(n, np.float32)
        if cfg.get("mute"):
            return W
        vw = None
        ramp = int(0.02 * sr)
        for si, sec in enumerate(self.sections):
            mode = (cfg.get("sections") or {}).get(str(si), "damaged" if cfg.get("use") != "all" else "stem")
            a, b = int(sec["start"] * sr), min(n, int(sec["end"] * sr))
            if b <= a or mode == "split":
                continue
            if mode == "damaged":
                if vw is None:
                    from scipy.ndimage import maximum_filter1d, uniform_filter1d
                    vw = np.clip(self._vocal_curve(0, n) * 4, 0, 1)
                    vw = uniform_filter1d(maximum_filter1d(vw, int(0.25 * sr)), int(0.04 * sr)).astype(np.float32)
                W[a:b] = vw[a:b]
            else:
                W[a:b] = 1.0 if mode == "stem" else 0.5
        for r in cfg.get("paint") or []:                 # painted on the lane: always (on) or never (off)
            a, b = max(0, int(r["start"] * sr)), min(n, int(r["end"] * sr))
            if b > a:
                W[a:b] = 1.0 if r.get("on") else 0.0
        from scipy.ndimage import uniform_filter1d
        return np.clip(uniform_filter1d(W, ramp), 0, 1).astype(np.float32)   # short crossfades at every change

    def _ref_offsets(self, ref, k, step=20.0):
        """How far your clean track sits from the split at points through the song: [(time, ms)] (later = +)."""
        from scipy import signal as sig
        sr = self.sr
        eo = VF.onset_env(self.stems[k].mean(0), sr)
        er = VF.onset_env(np.asarray(ref, np.float32).mean(0), sr)
        out = []
        win, lag = int(8000), 60
        for c in range(win, min(len(eo), len(er)) - win, int(step * 1000)):
            a, b = eo[c - win // 2:c + win // 2], er[c - win // 2 - lag:c + win // 2 + lag]
            if a.std() < 1e-6 or b.std() < 1e-6:
                continue
            xc = sig.correlate(b, a, mode="valid")
            if len(xc) != 2 * lag + 1 or xc.max() <= 0:
                continue
            out.append((c / 1000.0, float(np.argmax(xc) - lag)))
        return out

    def ref_find_timing(self, k):
        """The timing nudge (ms) that lines your clean track up best (median over the song)."""
        cfg = self.meta["refs"][k]
        old = cfg.get("nudge_ms", 0)
        cfg["nudge_ms"] = 0
        try:
            offs = self._ref_offsets(self.ref_audio(k), k)
        finally:
            cfg["nudge_ms"] = old
        return float(-np.median([o for _, o in offs])) if offs else 0.0

    def _fix_drift(self, ref, k):
        """A stem from a slightly different master can run a little fast or slow: fit the offsets along the song
        and stretch it to match, when the drift is clear (more than 4 ms end to end, and steady)."""
        offs = self._ref_offsets(ref, k)
        if len(offs) < 4:
            return ref, 0.0
        t = np.array([a for a, _ in offs]); o = np.array([b for _, b in offs])
        A = np.vstack([t, np.ones_like(t)]).T
        (slope, icpt), res, *_ = np.linalg.lstsq(A, o, rcond=None)
        span = slope * (t.max() - t.min())
        resid = o - (slope * t + icpt)
        if abs(span) < 4 or np.std(resid) > max(3.0, abs(span) / 3):
            return ref, 0.0
        n = ref.shape[1]
        tt = np.arange(n) / self.sr
        src = (tt + (slope * tt + icpt) / 1000.0) * self.sr          # where each output sample comes from
        out = np.stack([np.interp(src, np.arange(n), ref[c], left=0, right=0) for c in range(ref.shape[0])]).astype(np.float32)
        return out, float(slope * 1000)          # ms per 1000 s, roughly ppm

    def _tone_matched(self, rs, raw_seg, mask):
        """Your stem's tone matched to the split's where nobody sings (smooth EQ, within +-9 dB)."""
        if mask.sum() < self.sr // 4:
            return rs
        Er = np.abs(stft(rs.mean(0)[mask])) ** 2
        Es = np.abs(stft(raw_seg.mean(0)[mask])) ** 2
        g = np.sqrt((Es.mean(1) + 1e-10) / (Er.mean(1) + 1e-10))
        from scipy.ndimage import uniform_filter1d
        g = np.exp(uniform_filter1d(np.log(np.clip(g, 10 ** (-9 / 20), 10 ** (9 / 20))), 24))
        g = g / np.exp(np.mean(np.log(g)))                 # tone only; the level is matched separately
        out = np.empty_like(rs)
        for c in range(rs.shape[0]):
            out[c] = istft(stft(rs[c]) * g[:, None], rs.shape[1])
        return out

    def ref_disagree(self, k):
        """Where your clean track and the split play different things (notes, a missing part), where the split
        is reliable (no singing): [[start, end]]."""
        cfg = self.meta["refs"][k]
        key = (k, cfg.get("nudge_ms"), cfg.get("gain_db"))
        cache = self.__dict__.setdefault("_ref_dis", {})
        if key in cache:
            return cache[key]
        ref = self.ref_audio(k)
        Cr, Cs = self._beat_chroma(ref), self._beat_chroma(self.stems[k])
        va = np.asarray(self.VA, float) if len(self.VA) else np.zeros(len(Cr))
        cum = lambda y: np.r_[0, np.cumsum(np.asarray(y, np.float64).mean(0) ** 2)]
        bt = (np.asarray(self.beats, int) * HOP).clip(0, ref.shape[1])
        Er, Es = np.diff(cum(ref)[bt]), np.diff(cum(self.stems[k])[bt])
        floor = 1e-4 * max(float(np.median(Er[Er > 0])) if np.any(Er > 0) else 1, 1e-12)
        bad = []
        for j in range(min(len(Cr), len(Cs), len(Er))):
            if j < len(va) and va[j] > 0.15:
                continue
            if (Er[j] > floor) != (Es[j] > floor) or (Er[j] > floor and float(Cr[j] @ Cs[j]) < 0.55):
                bad.append(j)
        out, j0 = [], None
        for i, j in enumerate(bad):
            if j0 is None:
                j0 = j
            if i + 1 == len(bad) or bad[i + 1] != j + 1:
                if j + 1 - j0 >= 2:
                    out.append([round(self.beat_times[j0], 2), round(self.beat_times[min(j + 1, len(self.beat_times) - 1)], 2)])
                j0 = None
        cache[key] = out
        return out

    def ref_audio(self, k):
        """Your clean track as it's used: lined up, level, timing nudge applied."""
        ref = self.ref_track(k)
        cfg = (self.meta.get("refs") or {}).get(k)
        if ref is None or cfg is None:
            raise ValueError(f"No clean {k} track.")
        y = np.asarray(ref, np.float32) * float(10 ** (float(cfg.get("gain_db", 0)) / 20))
        sh = int(round(float(cfg.get("nudge_ms", 0)) * self.sr / 1000))
        if sh:
            y = np.roll(y, sh, axis=1)
            if sh > 0:
                y[:, :sh] = 0
            else:
                y[:, sh:] = 0
        return y

    def _rebuild_ref(self, k, status=None):
        """Remake the review items of your clean k track from its settings, one per section. status: what to set
        them to ("accepted" after you edit the lane, "pending" when first added; None keeps theirs)."""
        cfg = self.meta["refs"][k]
        sr, n = self.sr, self.original.shape[1]
        ref = self.ref_audio(k)
        raw = self.stems[k]
        Wf = self._ref_weight(k, cfg)
        ids = []
        for si, sec in enumerate(self.sections):
            a, b = int(sec["start"] * sr), min(n, int(sec["end"] * sr))
            rid = f"ref_{k}_{si}"
            lo, hi = max(0, a - XFADE), min(n, b + XFADE)
            if b - a < sr // 4:
                continue
            W = Wf[lo:hi].copy()
            t = np.arange(lo, hi) / sr                  # stay inside the section (neighbours have their own item)
            W *= np.minimum(np.clip((t - sec["start"] + 0.01) / 0.02, 0, 1), np.clip((sec["end"] + 0.01 - t) / 0.02, 0, 1))
            rs = ref[:, a:b]
            if float(W.max()) < 0.01 or float((rs ** 2).mean()) < 1e-9:
                if rid in self.suggestions:
                    self.suggestions[rid]["status"] = "rejected"
                    self.suggestions[rid].pop("by_build", None)
                continue
            vc = self._vocal_curve(a, b) < 0.1            # level: match the instrument where nobody sings
            m = vc if vc.sum() > sr // 4 else np.ones(b - a, bool)
            if cfg.get("tone_match", True):
                try:
                    seg = self._tone_matched(ref[:, lo:hi], raw[:, lo:hi], np.r_[np.zeros(a - lo, bool), m, np.zeros(hi - b, bool)])
                    ref[:, lo:hi] = seg
                    rs = ref[:, a:b]
                except Exception as e:
                    log(f"tone match skipped: {e}")
            gl = float(10 ** (float(cfg.get("gain_db", 0)) / 20))      # your level setting rides on top of the match
            g = float(np.clip(np.sqrt(float((raw[:, a:b][:, m] ** 2).mean()) / (float(((rs / gl)[:, m] ** 2).mean()) + 1e-12)), 0.25, 4))
            mode = (cfg.get("sections") or {}).get(str(si), "damaged")
            old = self.suggestions.get(rid, {})
            self.suggestions[rid] = {
                "id": rid, "type": "alt", "stem": k, "status": status or old.get("status", "pending"),
                "start": sec["start"], "end": sec["end"], "section": si, "conf": cfg.get("match", 1.0), "severity": 0.0,
                "strength": 1.0, "donors": [], "alt_mode": "imported", "blend": 0.0, "reference": cfg["name"],
                "use": cfg.get("use"),
                "note": f"Your clean \"{cfg['name']}\" for the {k} here: "
                        + {"damaged": "where the singing damaged it", "stem": "the whole part", "mix": "mixed half and half with the split",
                           "split": "not used"}.get(mode, "") + (" (with what you painted on its lane)" if cfg.get("paint") else "")}
            if status:
                self.suggestions[rid].pop("by_build", None)
            self.patches[rid] = (lo, (W[None, :] * (ref[:, lo:hi] * g - raw[:, lo:hi])).astype(np.float16))
            self.take_w[rid] = (lo, W.astype(np.float16))
            ids.append(rid)
        self._render_cache.pop(("stem", k), None)
        self._render_cache.pop("mix", None)
        return ids

    def ref_edit(self, k, ch):
        """Change your clean k track from its lane. ch: sections {si: mode} | paint {start, end, on} |
        clear_paint | gain_db | nudge_ms | mute."""
        with self.lock:
            cfg = (self.meta.get("refs") or {}).get(k)
            if cfg is None:
                raise ValueError(f"No clean {k} track.")
            if ch.get("auto_timing"):
                ch = {**ch, "nudge_ms": self.ref_find_timing(k)}
            self._push(f"Your clean {k} track", [i for i in self.suggestions if i.startswith(f"ref_{k}_")])
            for si, mode in (ch.get("sections") or {}).items():
                if mode in self.REF_MODES:
                    cfg.setdefault("sections", {})[str(si)] = mode
                    cfg["paint"] = [r for r in cfg.get("paint", [])            # a section choice resets painting there
                                    if not (r["start"] >= self.sections[int(si)]["start"] - 0.01
                                            and r["end"] <= self.sections[int(si)]["end"] + 0.01)]
            if ch.get("paint"):
                r = ch["paint"]
                a, b = sorted((float(r["start"]), float(r["end"])))
                if b - a >= 0.05:
                    keep = []
                    for q in cfg.get("paint", []):                 # the newest stroke wins where they overlap
                        if q["end"] <= a or q["start"] >= b:
                            keep.append(q)
                        else:
                            if q["start"] < a:
                                keep.append({**q, "end": a})
                            if q["end"] > b:
                                keep.append({**q, "start": b})
                    cfg["paint"] = sorted(keep + [{"start": round(a, 3), "end": round(b, 3), "on": bool(r.get("on", True))}],
                                          key=lambda q: q["start"])
            if ch.get("clear_paint"):
                cfg["paint"] = []
            if "gain_db" in ch:
                cfg["gain_db"] = float(np.clip(float(ch["gain_db"]), -24, 12))
            if "nudge_ms" in ch:
                cfg["nudge_ms"] = float(np.clip(float(ch["nudge_ms"]), -100, 100))
            if "mute" in ch:
                cfg["mute"] = bool(ch["mute"])
            if "tone_match" in ch:
                cfg["tone_match"] = bool(ch["tone_match"])
            return self._rebuild_ref(k, "accepted")

    def ref_lanes(self):
        """For the lanes: each clean track's settings and how much it's used, 10 times a second."""
        out = {}
        n = self.original.shape[1] if self.original is not None else 0
        rate = 10
        for k, cfg in (self.meta.get("refs") or {}).items():
            cols = n * rate // self.sr + 1
            use = np.zeros(cols, np.float32)
            for i, x in self.suggestions.items():
                if i.startswith(f"ref_{k}_") and x["status"] in ("accepted", "auto") and i in self.take_w:
                    lo, w = self.take_w[i]
                    w = np.asarray(w, np.float32)
                    step = self.sr // rate
                    m = len(w) // step
                    if m:
                        c0 = lo // step
                        seg = w[:m * step].reshape(m, step).mean(1)
                        use[c0:c0 + m] = np.maximum(use[c0:c0 + m], seg[:max(0, cols - c0)])
            try:
                dis = self.ref_disagree(k)
            except Exception as e:
                log(f"disagreement check skipped for {k}: {e}")
                dis = []
            out[k] = {**cfg, "rate": rate, "used": [round(float(v), 2) for v in use], "disagree": dis}
        return out

    # ------------------------------------------------------------------ drum hits you can see and edit
    def drum_map(self, k):
        """Every hit on a drum track, grouped into its sounds (rows): {"rows": [{id, name, count}],
        "hits": [[time, row id, level 0..1]]}."""
        cache = self.__dict__.setdefault("_drum_map", {})
        if k in cache:
            return cache[k]
        if k not in self.stems:
            raise ValueError(f"No {k} track.")
        x = self.stems[k].mean(0)
        if k not in self.PERCUSSIVE:
            try:
                cache[k] = self._note_map(k, x)
                return cache[k]
            except Exception as e:
                log(f"note rows skipped for {k}: {e}")
        sim = float((self.mixer.get("drum_sim") or {}).get(k, 0.9))
        pos, clusters, F, lv, centers = HT.find_sounds(x, self.sr, min_count=3, similarity=sim)
        rows, seen, hits = [], {}, []
        order = sorted(range(len(clusters)), key=lambda ci: -len(clusters[ci]))[:10]
        top = float(np.max(lv)) if len(lv) else 0.0
        Z = None
        if F is not None and len(F):
            Z = F - F.mean(1, keepdims=True)
            Z /= np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9
        for ci in order:
            c = clusters[ci]
            nm = HT.name_sound(F[c].mean(0), centers) if k in self.PERCUSSIVE else f"note {chr(65 + len(rows) % 26)}"
            seen[nm] = seen.get(nm, 0) + 1
            rows.append({"id": int(ci), "name": nm + (f" {seen[nm]}" if seen[nm] > 1 else ""), "count": len(c)})
            cen = Z[c].mean(0) if Z is not None else None
            if cen is not None:
                cen /= np.linalg.norm(cen) + 1e-9
            for i in c:
                fit = float(Z[i] @ cen) if cen is not None else 1.0       # how alike this hit is to its sound
                hits.append([round(float(pos[i]) / self.sr, 4), int(ci), round(float(np.clip(np.exp((lv[i] - top) / 2), 0, 1)), 3),
                             round(fit, 3)])
        hits.sort()
        cache[k] = {"rows": rows, "hits": hits, "_pos": pos, "_clusters": clusters, "_F": F, "_lv": lv, "sim": sim}
        return cache[k]

    def drum_set_similarity(self, k, sim):
        """How alike hits must be to count as the same sound (0.80 loose .. 0.97 strict). Your edits follow
        their sounds by name into the new grouping."""
        with self.lock:
            old = self.drum_map(k)
            names = {r["id"]: r["name"].rstrip(" 0123456789") for r in old["rows"]}
            self._push(f"Grid: {k} grouping", [])
            self.mixer.setdefault("drum_sim", {})[k] = float(np.clip(float(sim), 0.8, 0.97))
            self.__dict__.get("_drum_map", {}).pop(k, None)
            for key in list(self.__dict__.get("_drum_samples", {})):
                if key[0] == k:
                    self.__dict__["_drum_samples"].pop(key, None)
            new = self.drum_map(k)
            by_name = {}
            for r in new["rows"]:
                by_name.setdefault(r["name"].rstrip(" 0123456789"), r["id"])
            remap = {sid: by_name.get(nm) for sid, nm in names.items()}
            e = (self.mixer.get("drum_edits") or {}).get(k)
            if e:
                e["add"] = [{**a, "s": remap[a["s"]]} for a in e.get("add", []) if remap.get(a["s"]) is not None]
                e["samples"] = {str(remap[int(sid)]): v for sid, v in (e.get("samples") or {}).items()
                                if remap.get(int(sid)) is not None}
                e["assign"] = [{**a, "s": remap[a["s"]]} for a in e.get("assign", []) if remap.get(a["s"]) is not None]
            self._render_cache.pop(("stem", k), None)
            self._render_cache.pop("mix", None)
            return new

    def drum_odd_hits(self, k, sid=None, start=None, end=None, sure=0.9, fit_min=0.8):
        """Hits that are almost certainly a given sound but came out damaged or mislabelled: the sound plays at
        that place in the bar in at least `sure` of the song's bars, and the hit there doesn't sound like it (a
        poor match to its sound, or grouped as something else). Fills (the bar before a new section) are left alone.
        Returns [(time, sound id)]."""
        dm = self.drum_map(k)
        full = max(L for _, L in self.windows)
        bars = [(wi, s0) for wi, (s0, L) in enumerate(self.windows) if L == full and s0 + L < len(self.beat_times)]
        if len(bars) < 4:
            return []
        sec_start_bars = {sec.get("start_bar") for sec in self.sections}
        slot_of = {}
        for t, ci, lv, fit in dm["hits"]:
            j = int(np.searchsorted(self.beat_times, t, side="right")) - 1
            if j < 0 or j + 1 >= len(self.beat_times):
                continue
            q = int(round((t - self.beat_times[j]) / (self.beat_times[j + 1] - self.beat_times[j]) * 4))
            slot_of[(t, ci)] = (j, q)
        rows = [r["id"] for r in dm["rows"]] if sid is None else [sid]
        out = []
        for ci in rows:
            per_bar = {}
            for (t, c), (j, q) in slot_of.items():
                for wi, s0 in bars:
                    if s0 <= j < s0 + full:
                        per_bar.setdefault(wi, {}).setdefault(c, set()).add((j - s0) * 4 + q)
                        break
            counts = {}
            for wi, d in per_bar.items():
                for sl in d.get(ci, ()):
                    counts[sl] = counts.get(sl, 0) + 1
            steady = [sl for sl, n_ in counts.items() if n_ >= sure * len(bars)]
            if not steady:
                continue
            fit_of = {(h[0], h[1]): h[3] for h in dm["hits"]}
            for wi, s0 in bars:
                if (wi + 1) in sec_start_bars:
                    continue                                   # a fill before the next section: on purpose
                b0, b1 = self.beat_times[s0], self.beat_times[s0 + full]
                if start is not None and not (start <= b0 < end):
                    continue
                for sl in steady:
                    j, q = s0 + sl // 4, sl % 4
                    tt = self.beat_times[j] + q / 4 * (self.beat_times[j + 1] - self.beat_times[j])
                    near = [(t, c) for (t, c) in slot_of if abs(t - tt) < 0.045]
                    mine = [(t, c) for t, c in near if c == ci]
                    if mine:
                        t, c = mine[0]
                        if fit_of.get((t, c), 1) < fit_min:
                            out.append((t, ci))                # its own sound, but damaged
                    elif near:
                        out.append((near[0][0], ci))           # grouped as something else: it's this sound
        return sorted(set(out))

    def drum_repair_odd(self, k, sid=None, start=None, end=None):
        """Rebuild the odd hits (see drum_odd_hits) from the merge of their sound's best hits. Returns how many."""
        with self.lock:
            odd = self.drum_odd_hits(k, sid, start, end)
            if not odd:
                return 0
            self._push(f"Grid: {k} odd hits", [])
            e = self.mixer.setdefault("drum_edits", {}).setdefault(k, {"add": [], "off": [], "rebuild": []})
            e.setdefault("assign", [])
            dm = self.drum_map(k)
            pos = dm["_pos"] / self.sr
            for t, ci in odd:
                i = int(np.argmin(np.abs(pos - t)))
                nxt = pos[i + 1] if i + 1 < len(pos) else t + 0.3
                e["assign"] = [a for a in e["assign"] if abs(a["t"] - t) > 0.012] + [{"t": round(float(t), 4), "s": int(ci)}]
                e["rebuild"].append({"start": round(float(t) - 0.004, 4), "end": round(float(min(nxt, t + 0.3)) - 0.002, 4)})
            self._render_cache.pop(("stem", k), None)
            self._render_cache.pop("mix", None)
            return len(odd)

    NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

    def _note_map(self, k, x):
        """Pitched track: every note start and its pitch (harmonic product spectrum over the first 120 ms),
        rows = the notes it plays, high to low, like a piano roll."""
        sr = self.sr
        pos = HT.onsets(x, sr)
        if len(pos) < 4:
            raise ValueError("too few notes")
        N = 8192
        f = np.fft.rfftfreq(N, 1 / sr)
        win = np.hanning(int(0.12 * sr))
        lo_hz, hi_hz = (30, 400) if k == "bass" else (60, 1500)
        notes, lvs = [], []
        for p in pos:
            seg = x[int(p) + int(0.01 * sr):int(p) + int(0.01 * sr) + len(win)]
            if len(seg) < len(win):
                notes.append(-1); lvs.append(-30.0); continue
            S = np.abs(np.fft.rfft(seg * win, N))
            hps = S.copy()
            for h in (2, 3, 4):
                d = S[::h]
                hps[:len(d)] *= d
            band = (f >= lo_hz) & (f <= hi_hz)
            j = int(np.argmax(np.where(band, hps, 0)))
            if j <= 0:
                notes.append(-1); lvs.append(-30.0); continue
            notes.append(int(round(69 + 12 * np.log2(f[j] / 440.0))))
            lvs.append(float(np.log(np.mean(seg ** 2) + 1e-12)))
        notes, lvs = np.array(notes), np.array(lvs)
        counts = {}
        for m in notes[notes > 0]:
            counts[int(m)] = counts.get(int(m), 0) + 1
        keep = sorted([m for m, c in counts.items() if c >= 2], key=lambda m: -counts[m])[:24]
        keep.sort(reverse=True)
        rows, hits, clusters = [], [], []
        top = float(lvs.max()) if len(lvs) else 0.0
        for m in keep:
            idx = np.flatnonzero(notes == m)
            clusters.append(idx)
            rows.append({"id": len(rows), "name": f"{self.NOTE_NAMES[m % 12]}{m // 12 - 1}", "count": int(len(idx)), "midi": int(m)})
            for i in idx:
                hits.append([round(float(pos[i]) / sr, 4), len(rows) - 1, round(float(np.clip(np.exp((lvs[i] - top) / 2), 0, 1)), 3)])
        hits.sort()
        return {"rows": rows, "hits": hits, "_pos": pos, "_clusters": clusters, "_F": None, "_lv": lvs}

    def _drum_sample(self, k, sid):
        """The cleanest version of one drum sound: its clean hits (least singing), lined up and merged.
        Returns (audio (2, n) starting 3 ms before the attack, its level)."""
        cache = self.__dict__.setdefault("_drum_samples", {})
        if (k, sid) in cache:
            return cache[(k, sid)]
        dm = self.drum_map(k)
        pos, c, lv = dm["_pos"], dm["_clusters"][sid], dm["_lv"]
        x = self.stems[k]
        sr = self.sr
        pre = int(0.003 * sr)
        choice = (((self.mixer.get("drum_edits") or {}).get(k) or {}).get("samples") or {}).get(str(sid))
        if choice and choice.get("mode") == "file":
            try:
                y = np.load(os.path.join(self.root, "stems", f"_drum_{k}_{sid}.npy")).astype(np.float32)
                cache[(k, sid)] = (y, float(np.median(lv[c])))
                return cache[(k, sid)]
            except OSError:
                pass
        if choice and choice.get("mode") == "hit":       # one particular hit you picked
            i = int(np.argmin(np.abs(pos / sr - float(choice.get("t", 0)))))
            nx = int(pos[i + 1]) if i + 1 < len(pos) else x.shape[1]
            L = int(np.clip(nx - int(pos[i]), 0.06 * sr, 0.6 * sr))
            p0 = max(0, int(pos[i]) - pre)
            y = np.asarray(x[:, p0:p0 + L + pre], np.float32).copy()
            f = min(int(0.02 * sr), y.shape[1] // 4)
            y[:, -f:] *= np.linspace(1, 0, f, dtype=np.float32)
            cache[(k, sid)] = (y, float(lv[i]))
            return cache[(k, sid)]
        nxt = {int(p): int(q) for p, q in zip(pos, np.r_[pos[1:], x.shape[1]])}
        gaps = [nxt[int(pos[i])] - int(pos[i]) for i in c]
        L = int(np.clip(np.median(gaps) if gaps else 0.3 * sr, 0.06 * sr, 0.6 * sr))
        vc = self._vocal_curve(0, x.shape[1])
        med = float(np.median(lv[c]))
        ranked = sorted(c, key=lambda i: (vc[min(int(pos[i]) + int(0.02 * sr), len(vc) - 1)], abs(lv[i] - med)))
        clips = []
        for i in ranked[:5]:
            p = int(pos[i])
            if p - pre < 0 or p + L > x.shape[1]:
                continue
            seg = x[:, p - pre:p + L]
            if clips:
                sh = HT.align(clips[0].mean(0), seg.mean(0), int(0.003 * sr))
                q = p - pre - sh
                if q < 0 or q + clips[0].shape[1] > x.shape[1]:
                    continue
                seg = x[:, q:q + clips[0].shape[1]]
            clips.append(np.asarray(seg, np.float32))
        if not clips:
            raise ValueError("No clean example of that sound.")
        y = self._merge_clips(clips) if len(clips) >= 2 else clips[0].copy()
        f = min(int(0.02 * sr), y.shape[1] // 4)
        y[:, -f:] *= np.linspace(1, 0, f, dtype=np.float32)
        cache[(k, sid)] = (y, float(np.median([lv[i] for i in ranked[:5]])))
        return cache[(k, sid)]

    def drum_edits(self, k):
        return (self.mixer.get("drum_edits") or {}).get(k) or {"add": [], "off": [], "rebuild": []}

    def drum_edit(self, k, ch):
        """Edit the hits of drum track k. ch: add {t, s} | remove_add t | off t | on t | rebuild {start, end} |
        unrebuild {start, end} | clear {start, end} (your edits there)."""
        with self.lock:
            if not ch.get("_no_undo"):
                self._push(f"Grid: {k}", [])
            e = self.mixer.setdefault("drum_edits", {}).setdefault(k, {"add": [], "off": [], "rebuild": []})
            for key_ in ("vel", "move", "samples"):
                e.setdefault(key_, [] if key_ != "samples" else {})
            near = lambda a, b: abs(float(a) - float(b)) < 0.012
            if ch.get("add"):
                a = ch["add"]
                e["add"] = [x for x in e["add"] if not (near(x["t"], a["t"]) and x["s"] == int(a["s"]))]
                e["add"].append({"t": round(float(a["t"]), 4), "s": int(a["s"]), "v": float(a.get("v", 1.0))})
            if "remove_add" in ch:
                e["add"] = [x for x in e["add"] if not near(x["t"], ch["remove_add"])]
            if "off" in ch:
                if not any(near(t, ch["off"]) for t in e["off"]):
                    e["off"].append(round(float(ch["off"]), 4))
            if "on" in ch:
                e["off"] = [t for t in e["off"] if not near(t, ch["on"])]
            if ch.get("rebuild"):
                r = ch["rebuild"]
                e["rebuild"] = [q for q in e["rebuild"] if q["end"] <= r["start"] or q["start"] >= r["end"]]
                e["rebuild"].append({"start": round(float(r["start"]), 3), "end": round(float(r["end"]), 3)})
            if ch.get("unrebuild"):
                r = ch["unrebuild"]
                e["rebuild"] = [q for q in e["rebuild"] if q["end"] <= r["start"] or q["start"] >= r["end"]]
            if ch.get("vel"):                              # louder or softer: {t, v} (detected or added hit)
                t, v = float(ch["vel"]["t"]), float(np.clip(float(ch["vel"]["v"]), 0.05, 2.5))
                hit = False
                for x in e["add"]:
                    if near(x["t"], t):
                        x["v"] = round(v, 2)
                        hit = True
                if not hit:
                    e["vel"] = [x for x in e["vel"] if not near(x["t"], t)] + ([{"t": round(t, 4), "v": round(v, 2)}] if abs(v - 1) > 0.02 else [])
            if ch.get("paste"):                            # copied hits over these bars: replaces what's there
                r = ch["paste"]
                dm = self.drum_map(k)
                for t, _, _ in dm["hits"]:
                    if r["start"] <= t < r["end"] and not any(near(t, o) for o in e["off"]):
                        e["off"].append(round(t, 4))
                e["add"] = [x for x in e["add"] if not (r["start"] <= x["t"] < r["end"])]
                e["add"] += [{"t": round(float(h["t"]), 4), "s": int(h["s"]), "v": float(h.get("v", 1.0))} for h in r.get("hits", [])
                             if r["start"] <= float(h["t"]) < r["end"]]
                e["rebuild"] = [q for q in e["rebuild"] if q["end"] <= r["start"] or q["start"] >= r["end"]]
                e["rebuild"].append({"start": round(float(r["start"]), 3), "end": round(float(r["end"]), 3)})
            if ch.get("tighten"):                          # pull hits toward the grid by `amount` (0..1), rebuilt
                r = ch["tighten"]
                amt = float(np.clip(float(r.get("amount", 0.5)), 0, 1))
                bt = np.asarray(self.beat_times, float)
                q16 = np.concatenate([np.linspace(bt[j], bt[j + 1], 5)[:-1] for j in range(len(bt) - 1)]) if len(bt) > 1 else bt
                dm = self.drum_map(k)
                e["move"] = [x for x in e["move"] if not (r["start"] <= x["t"] < r["end"])]
                for t, _, _ in dm["hits"]:
                    if r["start"] <= t < r["end"] and len(q16):
                        g = float(q16[int(np.argmin(np.abs(q16 - t)))])
                        if abs(g - t) > 0.002:
                            e["move"].append({"t": round(t, 4), "to": round(t + amt * (g - t), 4)})
                for x in e["add"]:
                    if r["start"] <= x["t"] < r["end"] and len(q16):
                        g = float(q16[int(np.argmin(np.abs(q16 - x["t"])))])
                        x["t"] = round(x["t"] + amt * (g - x["t"]), 4)
                e["rebuild"] = [q for q in e["rebuild"] if q["end"] <= r["start"] or q["start"] >= r["end"]]
                e["rebuild"].append({"start": round(float(r["start"]), 3), "end": round(float(r["end"]), 3)})
            if ch.get("sample"):                           # which sound plays a row: merge | hit | file
                sm = ch["sample"]
                if sm.get("mode") in (None, "merge"):
                    e["samples"].pop(str(sm["s"]), None)
                else:
                    e["samples"][str(sm["s"])] = {kk: sm[kk] for kk in ("mode", "t", "name") if kk in sm}
                self.__dict__.get("_drum_samples", {}).pop((k, int(sm["s"])), None)
            if ch.get("clear"):
                r = ch["clear"]
                inside = lambda t: r["start"] <= float(t) < r["end"]
                e["add"] = [x for x in e["add"] if not inside(x["t"])]
                e["off"] = [t for t in e["off"] if not inside(t)]
                e["vel"] = [x for x in e["vel"] if not inside(x["t"])]
                e["move"] = [x for x in e["move"] if not inside(x["t"])]
                e["rebuild"] = [q for q in e["rebuild"] if q["end"] <= r["start"] or q["start"] >= r["end"]]
            if not (e["add"] or e["off"] or e["rebuild"] or e["vel"] or e["move"] or e["samples"]):
                self.mixer["drum_edits"].pop(k, None)
            self._render_cache.pop(("stem", k), None)
            self._render_cache.pop("mix", None)
            return self.drum_edits(k)

    def drum_load_sample(self, k, sid, audio, name):
        """Your own one-shot for one row of the grid."""
        y = np.asarray(audio, np.float32)
        if y.shape[0] == 1:
            y = np.repeat(y, 2, axis=0)
        on = HT.onsets(y.mean(0), self.sr)
        st_ = max(0, int(on[0]) - int(0.003 * self.sr)) if len(on) else 0
        y = y[:, st_:st_ + int(1.0 * self.sr)]
        dm = self.drum_map(k)
        c = dm["_clusters"][sid]
        ref = np.sqrt(np.mean([np.mean(self.stems[k][:, int(p):int(p) + int(0.05 * self.sr)] ** 2) for p in dm["_pos"][c][:20]]) + 1e-12)
        y *= float(ref / (np.sqrt(np.mean(y[:, :int(0.05 * self.sr)] ** 2)) + 1e-9))
        np.save(os.path.join(self.root, "stems", f"_drum_{k}_{sid}.npy"), y.astype(np.float32))
        return self.drum_edit(k, {"sample": {"s": sid, "mode": "file", "name": name}})

    def drum_midi_export(self, k, path):
        """The grid's hits (with your edits) as a MIDI file on the song's tempo map."""
        from . import midi as MI
        dm, e = self.drum_map(k), self.drum_edits(k)
        offs = e.get("off", [])
        vel = {round(x["t"], 3): x["v"] for x in e.get("vel", [])}
        gm = lambda nm: (36 if "kick" in nm else 38 if "snare" in nm or "clap" in nm else 42 if "hat" in nm
                         else 49 if "cymbal" in nm or "crash" in nm or "ride" in nm else 45 if "tom" in nm else 39)
        note_of = {}
        for r in dm["rows"]:
            note_of[r["id"]] = r.get("midi") or gm(r["name"].lower())
        notes = []
        for t, sid, lv in dm["hits"]:
            if any(abs(t - o) < 0.012 for o in offs):
                continue
            v = vel.get(round(t, 3), 1.0)
            notes.append((t, note_of.get(sid, 39), int(np.clip(40 + 87 * lv * v, 1, 127)), 0.1))
        for ad in e.get("add", []):
            notes.append((ad["t"], note_of.get(ad["s"], 39), int(np.clip(100 * ad.get("v", 1.0), 1, 127)), 0.1))
        ch = 9 if k in self.PERCUSSIVE else 0
        MI.write(path, self.beat_times, markers=[(sc["start"], sc["name"]) for sc in self.sections],
                 tracks=[(k, ch, sorted(notes))], name=f"{self.meta.get('name', 'song')} - {k}")
        return path

    def drum_midi_import(self, k, data):
        """Play a MIDI file's notes with this track's own clean sounds: drums by General MIDI note, pitched
        instruments by note. Replaces what's in the bars the file covers. Returns how many notes were used."""
        from . import midi as MI
        notes = MI.read_notes(data)
        if not notes:
            raise ValueError("No notes in that MIDI file.")
        dm = self.drum_map(k)
        gm_name = lambda n: ("kick" if n in (35, 36) else "snare" if n in (37, 38, 39, 40) else "hat" if n in (42, 44, 46)
                             else "cymbal" if n in (49, 51, 52, 53, 55, 57, 59) else "tom" if n in (41, 43, 45, 47, 48, 50) else "")
        hits = []
        for t, n, v in notes:
            sid = None
            for r in dm["rows"]:
                if r.get("midi") is not None:
                    if r["midi"] == n:
                        sid = r["id"]
                elif gm_name(n) and gm_name(n) in r["name"].lower():
                    sid = r["id"]
                if sid is not None:
                    break
            if sid is not None:
                hits.append({"t": t, "s": sid, "v": round(max(0.1, v / 100), 2)})
        if not hits:
            raise ValueError("None of the file's notes match a sound on this track.")
        a, b = min(h["t"] for h in hits), max(h["t"] for h in hits) + 0.25
        bt = np.asarray(self.beat_times, float)
        bars = [self.beat_times[s0] for s0, L in self.windows]
        a = max([x for x in bars if x <= a] or [0.0])
        b = min([x for x in bars if x >= b] or [float(self.meta.get("duration", b))])
        self.drum_edit(k, {"paste": {"start": a, "end": b, "hits": hits}})
        return len(hits)

    def drum_fill_pattern(self, k, start, end):
        """Add the hits the learned pattern is sure belong in [start, end) and the track is missing."""
        found = self.find_pattern_hits()
        self._push(f"Grid: {k} pattern", [])
        n = 0
        for (kk, sid), ps in found.items():
            if kk != k:
                continue
            for p in ps:
                t = p / self.sr
                if start <= t < end:
                    self.drum_edit(k, {"add": {"t": t, "s": sid}, "_no_undo": True})
                    n += 1
        return n

    def _apply_drum_edits(self, k, y):
        e = (self.mixer.get("drum_edits") or {}).get(k)
        if not e:
            return y
        sr, n = self.sr, y.shape[1]
        dm = self.drum_map(k)
        pos = dm["_pos"]
        pre = int(0.003 * sr)
        lvl_of = {}
        for ci, c in enumerate(dm["_clusters"]):
            for i in c:
                lvl_of[int(pos[i])] = (ci, float(dm["_lv"][i]))
        def place(out, t, sid, gain=1.0, lv=None):
            try:
                smp, slv = self._drum_sample(k, sid)
            except Exception:
                return
            if lv is not None:
                gain *= float(np.clip(np.exp((lv - slv) / 2), 0.1, 4))
            a = int(round(t * sr)) - pre
            a0, a1 = max(0, a), min(n, a + smp.shape[1])
            if a1 > a0:
                out[:, a0:a1] += smp[:, a0 - a:a1 - a] * gain
        y = y.copy()
        nxt = np.r_[pos[1:], n] if len(pos) else pos
        def take_out(i, amount=1.0):
            """Remove (part of) one hit's sound, not everything under it: its clean sample, lined up and
            level-matched, is subtracted frequency by frequency, so a cymbal ringing underneath stays."""
            p = int(pos[i])
            ci, lv = lvl_of.get(p, (None, None))
            a, b = max(0, p - pre), min(n, int(nxt[i]) + int(0.05 * sr), p + int(0.6 * sr))
            if ci is None or b - a < 256:
                return
            try:
                smp, slv = self._drum_sample(k, ci)
            except Exception:
                return
            L = min(b - a, smp.shape[1])
            sh = HT.align(smp[:, :L].mean(0), y[:, a:a + L].mean(0), int(0.003 * sr))
            a2 = int(np.clip(a - sh, 0, n - L))
            g = float(np.clip(np.exp((lv - slv) / 2), 0.1, 4)) * amount
            for c in range(y.shape[0]):
                Y = stft(y[c, a2:a2 + L])
                S = np.abs(stft(smp[c, :L] * g))
                mag = np.maximum(np.abs(Y) - S, (0.05 if amount >= 1 else 0.3) * np.abs(Y))
                y[c, a2:a2 + L] = istft(mag * np.exp(1j * np.angle(Y)), L)
        offs = e.get("off", [])
        moves = {round(x["t"], 3): x["to"] for x in e.get("move", [])}
        vels = {round(x["t"], 3): x["v"] for x in e.get("vel", [])}
        in_rebuild = lambda t: any(q["start"] <= t < q["end"] for q in e.get("rebuild", []))
        for t in offs:
            if len(pos) and not in_rebuild(t):
                take_out(int(np.argmin(np.abs(pos / sr - t))))
        for tk, v in vels.items():                   # louder or softer detected hits
            if not len(pos) or in_rebuild(tk):
                continue
            i = int(np.argmin(np.abs(pos / sr - tk)))
            if v < 1:
                take_out(i, 1 - v)
            elif int(pos[i]) in lvl_of:
                ci, lv = lvl_of[int(pos[i])]
                place(y, pos[i] / sr, ci, v - 1, lv)
        for q in e.get("rebuild", []):               # these bars made again from clean samples, hit by hit
            a, b = max(0, int(q["start"] * sr)), min(n, int(q["end"] * sr))
            if b - a < 64:
                continue
            synth = np.zeros((2, n), np.float32)
            for p in pos:
                t = p / sr
                if q["start"] <= t < q["end"] and not any(abs(t - o) < 0.012 for o in offs) and int(p) in lvl_of:
                    ci, lv = lvl_of[int(p)]
                    re_ = next((a for a in e.get("assign", []) if abs(a["t"] - t) < 0.012), None)
                    if re_ is not None:                      # repaired as the sound it really is, at its usual level
                        ci = re_["s"]
                        try:
                            lv = self._drum_sample(k, ci)[1]
                        except Exception:
                            pass
                    place(synth, moves.get(round(t, 3), t), ci, vels.get(round(t, 3), 1.0), lv)
            for ad in e.get("add", []):
                if q["start"] <= ad["t"] < q["end"]:
                    place(synth, ad["t"], ad["s"], ad.get("v", 1.0))
            f = min(int(0.01 * sr), (b - a) // 4)
            w = np.ones(b - a, np.float32)
            w[:f] = np.linspace(0, 1, f)
            w[-f:] = np.linspace(1, 0, f)
            y[:, a:b] = y[:, a:b] * (1 - w)[None, :] + synth[:, a:b] * w[None, :]
        for ad in e.get("add", []):                  # added hits outside rebuilt bars
            if not any(q["start"] <= ad["t"] < q["end"] for q in e.get("rebuild", [])):
                place(y, ad["t"], ad["s"], ad.get("v", 1.0))
        return y

    def add_reference_sample(self, audio, name):
        """A clean one-shot (one drum hit or one note): find the repeated sound in the song it is, and rebuild that
        sound's damaged hits (and the ones the pattern says the vocals took) from it. Returns (ids, what)."""
        with self.lock:
            sr = self.sr
            smp = np.asarray(audio, np.float32)
            if smp.shape[0] == 1:
                smp = np.repeat(smp, 2, axis=0)
            on = HT.onsets(smp.mean(0), sr)
            st = int(on[0]) if len(on) else int(np.argmax(np.abs(smp.mean(0)) > 0.1 * np.abs(smp).max()))
            smp = smp[:, max(0, st - int(0.003 * sr)):]
            B, _ = HT._bands(sr)
            F, _ = HT._hit_features(smp.mean(0), np.array([int(0.003 * sr)]), sr, B)
            f = F[0] - F[0].mean()
            best = None
            for k in self.instruments:
                if k not in self.PERCUSSIVE and not self._tonal(k):
                    continue
                try:
                    sd = self._sounds(k)
                except Exception:
                    continue
                for snd in sd["sounds"]:
                    sh = sd["F"][snd.get("members") or [snd["best"]]].mean(0)
                    sh = sh - sh.mean()
                    cos = float(f @ sh / (np.linalg.norm(f) * np.linalg.norm(sh) + 1e-12))
                    if best is None or cos > best[0]:
                        best = (cos, k, snd)
            if best is None or best[0] < 0.75:
                raise ValueError(f"\"{name}\" doesn't sound like any of the repeated hits or notes in this song "
                                 f"(closest match {0 if best is None else best[0]:.0%}).")
            cos, k, snd = best
            sd = self._sounds(k)
            raw = self.stems[k]
            n = raw.shape[1]
            L = int(min(smp.shape[1], (0.35 if k in self.PERCUSSIVE else 0.5) * sr))
            pad = int(sr)
            # the sample sits after the song in an extended copy, so the repair engine can borrow from it
            ext = np.concatenate([raw, np.zeros((2, pad), np.float32), smp[:, :L + 4096]], axis=1)
            vext = np.concatenate([self.stems["vocals"], np.zeros((2, ext.shape[1] - n), np.float32)], axis=1)
            iext = np.concatenate([self.inst_sum, np.zeros((2, ext.shape[1] - n), np.float32)], axis=1)
            sp = n + pad
            pre = int(0.003 * sr)
            targets = [int(sd["pos"][i]) for i in snd["damaged"]]
            try:
                targets += self.find_pattern_hits().get((k, snd["id"]), [])
            except Exception:
                pass
            by_sec = {}
            for q in sorted(set(targets)):
                by_sec.setdefault(self._section_of(q / sr), []).append(q)
            ids = []
            for si, qs in by_sec.items():
                if si is None:
                    continue
                sec = self.sections[si]
                a, b = max(0, int(sec["start"] * sr) - 2048), min(n, int(sec["end"] * sr) + int(0.4 * sr))
                patch = np.zeros((2, b - a), np.float32)
                used = 0
                for q in qs:
                    Lq = min(L, int(0.25 * sr))
                    if q - pre - MARGIN < a or q + Lq + MARGIN > b:
                        continue
                    shf = HT.align(ext[:, sp:sp + pre + Lq].mean(0), self.original[:, q - pre:q + Lq].mean(0), int(0.002 * sr))
                    res = repair_window(ext, vext, iext, q - pre, q + Lq, [(sp + pre - q) - shf], 1.0, 1.0,
                                        self.B, self.centers, sr, self._level_floor(k))
                    if res["severity"] < 0.02:
                        continue
                    lo, pp = res["lo"], res["patch"]
                    o0, o1 = max(lo, a), min(lo + pp.shape[1], b)
                    patch[:, o0 - a:o1 - a] += pp[:, o0 - lo:o1 - lo]
                    used += 1
                if not used:
                    continue
                rid = f"smp_{k}_{snd['id']}_{si}"
                self.suggestions[rid] = {
                    "id": rid, "type": "hits", "stem": k, "sound": snd["id"], "status": "pending", "pattern": True,
                    "start": sec["start"], "end": sec["end"], "section": si, "conf": round(cos, 2), "severity": 0.0,
                    "strength": 1.0, "donors": [], "reference": name,
                    "note": f"Rebuilds {used} {snd['name']} hit{'s' if used != 1 else ''} from your sample \"{name}\" "
                            f"({cos:.0%} match), lined up with the original"}
                self.patches[rid] = (a, patch.astype(np.float16))
                ids.append(rid)
            self.check_ids(ids)
            return ids, f"the {snd['name']} on the {k} ({cos:.0%} match)"

    # ------------------------------------------------------------------ the singer's pitch
    def _vocal_f0(self, V, sr):
        """Fundamental frequency of the singing per STFT frame of a vocal magnitude spectrum V (bins, frames),
        and how voiced each frame is (0..1). Harmonic sum over a grid of candidate pitches."""
        bw = sr / N_FFT
        cands = np.geomspace(80.0, 1000.0, 160)
        L = np.log(V + 1e-6)
        S = np.zeros((len(cands), V.shape[1]))
        for h in range(1, 7):
            bins = cands * h / bw
            ok = bins < V.shape[0] - 1
            i0 = np.floor(bins[ok]).astype(int)
            fr = (bins[ok] - i0)[:, None]
            S[ok] += (1 - fr) * L[i0] + fr * L[i0 + 1]
        best = np.argmax(S, axis=0)
        f0 = cands[best]
        # voiced: harmonics hold a good share of the frame's energy between 80 Hz and 5 kHz
        freqs = np.arange(V.shape[0]) * bw
        band = (freqs >= 80) & (freqs <= 5000)
        E = (V ** 2)
        tot = E[band].sum(0) + 1e-12
        harm = np.zeros(V.shape[1])
        for h in range(1, 9):
            b = np.clip(np.round(f0 * h / bw).astype(int), 0, V.shape[0] - 2)
            harm += E[b, np.arange(V.shape[1])] + E[b + 1, np.arange(V.shape[1])]
        voiced = np.clip((harm / tot - 0.15) / 0.35, 0, 1)
        loud = 20 * np.log10(np.sqrt(tot) + 1e-12)
        voiced *= (loud > np.percentile(loud, 99) - 45)
        return f0, voiced

    def _pitch_gain(self, V, sr, g, lo, hi):
        """Gain (bins, frames): narrow notches on the singer's harmonics, deeper where the singer is louder."""
        f0, voiced = self._vocal_f0(V, sr)
        # reverb keeps ringing on the last note sung: hold it, fading, for up to ~0.8 s
        dec = float(np.exp(-(HOP / sr) / (0.8 / 3)))
        hf, hv = 0.0, 0.0
        for i in range(len(f0)):
            if voiced[i] >= hv * dec:
                hf, hv = f0[i], voiced[i]
            else:
                hv *= dec
                f0[i], voiced[i] = hf, hv
        bw = sr / N_FFT
        nb, nf = V.shape
        cut = np.zeros((nb, nf), np.float32)
        vmax = V.max(0) + 1e-9
        cols = np.arange(nf)
        for h in range(1, 25):
            fh = f0 * h
            if (fh > hi * 1.25).all():
                break
            sigma = np.maximum(1.2 * bw, 0.015 * fh)
            c = np.round(fh / bw).astype(int)
            strength = np.sqrt(np.clip(V[np.clip(c, 0, nb - 1), cols] / vmax, 0, 1))
            amt = strength * ((fh >= lo * 0.8) & (fh <= hi * 1.25)) * voiced
            for d in range(-4, 5):                     # only the few bins around each harmonic
                idx = c + d
                ok = (idx >= 0) & (idx < nb) & (amt > 0)
                if not ok.any():
                    continue
                val = np.exp(-0.5 * ((idx[ok] * bw - fh[ok]) / sigma[ok]) ** 2) * amt[ok]
                cut[idx[ok], cols[ok]] = np.maximum(cut[idx[ok], cols[ok]], val)
        return 1 - (1 - g) * cut

    # ------------------------------------------------------------------ lower frequencies in part of the song
    EQ_PRESETS = {"vocal": (300.0, 3500.0), "ess": (5000.0, 10000.0), "harsh": (2000.0, 5000.0),
                  "mud": (150.0, 500.0), "boom": (60.0, 150.0)}

    def _apply_eq_ranges(self, k, seg, a, b):
        """Frequency cuts the person set for part of the song ("Lower frequencies here"). Two kinds:
        fixed: a band lowered by a set amount; follow: only the frequencies the singer is using at each
        moment, which takes out a ghost vocal while leaving the rest of the instrument alone. Faded in and
        out over 30 ms, optionally only while someone sings."""
        ranges = [r for r in (self.mixer.get("eq_ranges") or [])
                  if r.get("stem") in (None, "", k) and r["start"] * self.sr < b and r["end"] * self.sr > a]
        if not ranges:
            return seg
        from scipy import signal as sig
        from scipy.ndimage import gaussian_filter
        out = np.array(seg, np.float32, copy=True)
        sr, ramp = self.sr, 0.03
        nyq = sr / 2
        for r in ranges:
            s0, s1 = max(a, int((r["start"] - ramp) * sr)), min(b, int((r["end"] + ramp) * sr))
            if s1 - s0 < 256:
                continue
            lo, hi = (self.EQ_PRESETS.get(r.get("preset")) or (float(r.get("lo", 300)), float(r.get("hi", 3500))))
            lo, hi = max(20.0, min(lo, hi)), min(nyq * 0.95, max(lo, hi))
            g = 10 ** (min(0.0, float(r.get("db", -6))) / 20)
            t = np.arange(s0, s1) / sr
            w = np.minimum(np.clip((t - r["start"] + ramp / 2) / ramp, 0, 1),
                           np.clip((r["end"] + ramp / 2 - t) / ramp, 0, 1)).astype(np.float32)
            if r.get("only_vocals", True):
                vw = self._vocal_curve(s0, s1)
                if r.get("tails", True):                      # and the reverb ringing on after each line
                    vw = np.maximum(vw, self._vocal_tail(s0, s1))
                w = w * vw
            if not w.any():
                continue
            x = out[:, s0 - a:s1 - a]
            # 2) centre only: the lead vocal sits in the middle of the mix, most instruments are spread out,
            #    so cutting only the middle (mid) leaves the stereo sides of the instrument untouched
            centre = bool(r.get("centre")) and x.shape[0] == 2
            if centre:
                side = (x[0] - x[1]) * 0.5
                x = ((x[0] + x[1]) * 0.5)[None, :].copy()
            if r.get("mode") == "pitch":
                # notch exactly the notes the singer is singing (and their overtones), nothing in between.
                # It depends only on the singing, so it's worked out once per range and kept.
                gcache = self.__dict__.setdefault("_eq_gain_cache", {})
                gkey = ("pitch", json.dumps(r, sort_keys=True), s0, s1)
                if gkey not in gcache:
                    V = np.abs(stft(self.stems["vocals"][:, s0:s1].mean(0)))
                    plo = 80.0 if r.get("preset") in (None, "vocal") else lo
                    G = self._pitch_gain(V, sr, g, plo, hi)
                    wf = np.interp(np.arange(V.shape[1]) * HOP, np.arange(len(w)), w)
                    gcache[gkey] = (1 - (1 - G) * wf[None, :]).astype(np.float32)
                    if len(gcache) > 64:
                        gcache.pop(next(iter(gcache)))
                G = gcache[gkey]
                for ch in range(x.shape[0]):
                    X = stft(x[ch])
                    m = min(X.shape[1], G.shape[1])
                    X[:, :m] *= G[:, :m]
                    x[ch] = istft(X, x.shape[1])
            elif r.get("mode") == "follow":
                # cut each frequency by how much the singer is using it at that moment (within the band)
                gcache = self.__dict__.setdefault("_eq_gain_cache", {})
                gkey = ("follow", json.dumps(r, sort_keys=True), s0, s1)
                if gkey not in gcache:
                    V = np.abs(stft(self.stems["vocals"][:, s0:s1].mean(0)))
                    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
                    band = np.clip(np.minimum((freqs - lo * 0.8) / (lo * 0.2 + 1e-9), (hi * 1.25 - freqs) / (hi * 0.25 + 1e-9)), 0, 1)
                    ref = np.percentile(V[band > 0.5], 95) + 1e-9 if (band > 0.5).any() else V.max() + 1e-9
                    use = gaussian_filter(np.clip(V / ref, 0, 1), (1.5, 1.0)) * band[:, None]
                    wf = np.interp(np.arange(V.shape[1]) * HOP, np.arange(len(w)), w)
                    gcache[gkey] = (1 - (1 - g) * use * wf[None, :]).astype(np.float32)
                    if len(gcache) > 64:
                        gcache.pop(next(iter(gcache)))
                G = gcache[gkey]
                for ch in range(x.shape[0]):
                    X = stft(x[ch])
                    m = min(X.shape[1], G.shape[1])
                    X[:, :m] *= G[:, :m]
                    x[ch] = istft(X, x.shape[1])
            else:
                f = [0.0, lo * 0.8, lo, hi, min(hi * 1.25, nyq * 0.99), nyq]
                gains = [1.0, 1.0, g, g, 1.0, 1.0]
                keep = [0] + [i for i in range(1, len(f)) if f[i] > f[i - 1] + 1e-6]
                taps = sig.firwin2(1025, [f[i] / nyq for i in keep], [gains[i] for i in keep])
                for ch in range(x.shape[0]):
                    filt = sig.oaconvolve(x[ch], taps, mode="same").astype(np.float32)
                    x[ch] = x[ch] + (filt - x[ch]) * w
            if centre:
                mid = x[0]
                x = np.stack([mid + side, mid - side])
            out[:, s0 - a:s1 - a] = x
        return out

    def _vocal_tail(self, a, b, release=0.8):
        """0..1 for [a, b): the singing itself and its reverb tail, which rings on for up to `release` seconds
        after each line ends (decaying). Measured from the vocal track every 10 ms."""
        if "vocals" not in self.stems:
            return np.zeros(b - a, np.float32)
        if "_tail" not in self.__dict__:
            v = self.stems["vocals"].mean(0)
            hop = max(1, self.sr // 100)
            m = len(v) // hop
            rms = np.sqrt((v[:m * hop].reshape(m, hop) ** 2).mean(1) + 1e-12)
            db = 20 * np.log10(rms + 1e-12)
            sung = np.clip((db - (np.percentile(db, 99) - 40)) / 10, 0, 1)
            dec = float(np.exp(-0.01 / (release / 3)))
            env = np.empty(m, np.float32)
            cur = 0.0
            for i in range(m):                                  # hold and decay after each line
                cur = max(float(sung[i]), cur * dec)
                env[i] = cur
            self.__dict__["_tail"] = (hop, env)
        hop, env = self.__dict__["_tail"]
        idx = np.clip(np.arange(a, b) // hop, 0, len(env) - 1)
        return env[idx]

    # ------------------------------------------------------------------ full tone match under the vocals
    def _toned(self, k):
        """The instrument with its tone-match curve applied under the singing (kept parts untouched).
        A linear-phase filter with the curve's response, blended in by how much singing there is, so it
        works on the whole song with little memory. Kept until the instrument or its curve changes."""
        curve = tuple(round(float(v), 2) for v in self.mixer.get("tone_eq", {}).get(k, ()))

        def make():
            from scipy import signal as sig
            y = self.render_stem(k)
            n = y.shape[1]
            nyq = self.sr / 2
            c = np.asarray(self.centers, np.float64)
            g = 10 ** (np.asarray(curve, np.float64) / 20)
            f = np.r_[0.0, np.clip(c / nyq, 1e-4, 0.999), 1.0]
            gg = np.r_[g[0], g, g[-1]]
            keep = np.r_[True, np.diff(f) > 1e-6]               # firwin2 needs rising frequencies
            taps = sig.firwin2(2049, f[keep], gg[keep])
            w = self._vocal_curve(0, n)
            kept = self.keep_mask(0, n)
            if kept is not None:
                w = w * (1 - kept)
            out = np.empty_like(y)
            for ch in range(y.shape[0]):
                filt = sig.oaconvolve(y[ch], taps, mode="same").astype(np.float32)
                out[ch] = y[ch] + (filt - y[ch]) * w
            return out
        if not curve:
            return self.render_stem(k)
        return self._stem_cached("toned", k, (curve, json.dumps([self.mixer.get("keep", []), self.mixer.get("seams", [])], sort_keys=True)), make)

    def tone_curve(self, k):
        """dB per band (low to high) the instrument still differs under the vocals from the same part
        where nobody sings (its matched sources): positive means it needs lifting there. Smoothed across
        bands, small differences left alone, limited to +-6 dB."""
        if self.stage != "detected" or k not in self.stem_feats:
            return None
        from scipy.ndimage import gaussian_filter1d
        y = self.shaped_stem(k)
        E = VF.band_energy(y.mean(0), self.B)
        F, _, _ = A.beat_features(E, np.zeros((12, E.shape[1]), np.float32), np.zeros_like(E), self.beats)
        _, _, R = self.stem_feats[k]
        num = np.zeros(F.shape[1])
        den = np.zeros(F.shape[1])
        for x in list(self.suggestions.values()):
            if x["stem"] != k or x["type"] != "repair" or not x.get("donors"):
                continue
            a, L, dn = x["beat"], x["beats"], x["donors"][0]["beat"]
            Rt, Rd, Ft, Fd = R[a:a + L], R[dn:dn + L], F[a:a + L], F[dn:dn + L]
            if Rt.shape != Rd.shape or Ft.shape != Fd.shape:
                continue
            rel, cov = (1 - Rt) * (1 - Rd), Rt * (1 - Rd)
            if rel.sum() < 0.5:
                continue
            off = ((Fd - Ft) * rel).sum() / rel.sum()           # the two bars' overall level difference
            num += ((Fd - Ft - off) * cov).sum(0)
            den += cov.sum(0)
        if den.sum() < 2:
            return None
        db = np.where(den > 0.5, num / np.maximum(den, 1e-9), 0.0) * 10 / np.log(10)
        db = gaussian_filter1d(db, 1.5, mode="nearest")
        db = np.where(np.abs(db) < 0.75, 0.0, db)
        return np.clip(db, -6, 2)          # lifting more brings up the separation noise under the vocals

    def mud_excess(self, k, lazy=False):
        """dB of extra low-mid (150-500 Hz) the instrument has under the vocals, next to the same part
        where nobody sings (its matched sources), so arrangement changes cancel out. Positive = muddy."""
        if self.stage != "detected" or k not in self.stem_feats:
            return 0.0
        extra = (self.mixer.get("vocal_lift", {}).get(k), self.mixer.get("mud_cut", {}).get(k), len(self.suggestions))
        return self._stem_cached("mud", k, extra, lambda: self._mud_excess(k), lazy)

    def _mud_excess(self, k):
        y = self.shaped_stem(k)
        E = VF.band_energy(y.mean(0), self.B)
        F, _, _ = A.beat_features(E, np.zeros((12, E.shape[1]), np.float32), np.zeros_like(E), self.beats)
        _, _, R = self.stem_feats[k]
        mud = (np.asarray(self.centers) >= self.MUD_BAND[0]) & (np.asarray(self.centers) <= self.MUD_BAND[1])
        vals, wts = [], []
        for x in self.suggestions.values():
            if x["stem"] != k or x["type"] != "repair" or not x.get("donors"):
                continue
            a, L, dn = x["beat"], x["beats"], x["donors"][0]["beat"]
            Rt, Rd, Ft, Fd = R[a:a + L], R[dn:dn + L], F[a:a + L], F[dn:dn + L]
            if Rt.shape != Rd.shape or Ft.shape != Fd.shape:
                continue
            rel, cov = (1 - Rt) * (1 - Rd), Rt * (1 - Rd)
            covm = cov[:, mud]
            if covm.sum() < 0.5 or rel.sum() < 0.5:
                continue
            off = ((Fd - Ft) * rel).sum() / rel.sum()          # level difference of the two bars overall
            vals.append((((Ft - Fd)[:, mud] + off) * covm).sum() / covm.sum() * 10 / np.log(10))
            wts.append(covm.sum())
        return float(np.average(vals, weights=wts)) if vals else 0.0

    def remaining_loss(self, k, lazy=False):
        """Estimated dB the instrument is still missing under the vocals, measured against its matched
        sources (same part, vocal-free) so arrangement changes cancel out."""
        if self.stage != "detected" or k not in self.stem_feats:
            return 0.0
        extra = (self.mixer.get("vocal_lift", {}).get(k), self.mixer.get("mud_cut", {}).get(k), len(self.suggestions))
        return self._stem_cached("loss", k, extra, lambda: self._remaining_loss(k), lazy)

    def _remaining_loss(self, k):
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

        if sug["type"] in ("take", "alt", "hits", "bars") and mode in ("before", "after", "patch"):
            def render(status):
                old = sug["status"]
                sug["status"] = status
                self._render_cache.pop(("stem", k), None)
                try:
                    if hear == "solo":
                        return self.shaped_stem(k, a, b).copy()
                    y = self.mix("repaired", region=(a, b))
                    return y + self.stems["vocals"][:, a:b] if hear == "song" else y
                finally:
                    sug["status"] = old
                    self._render_cache.pop(("stem", k), None)
            before, after = render("rejected"), render("accepted")
            return {"before": before, "after": after, "patch": after - before}[mode]

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
                 "patch": self.patches.get(i), "take_w": self.take_w.get(i)} for i in ids if i in self.suggestions]

    def _push(self, label, ids):
        self.history.append({"label": label, "items": self._snapshot(ids), "mixer": json.loads(json.dumps(self.mixer)),
                             "refs": json.loads(json.dumps(self.meta.get("refs") or {}))})
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
            elif sug["type"] == "rebuild" and "beat" in sug and sug.get("donors"):
                F, C, R = self.stem_feats[sug["stem"]]
                donors = [{"beat": d["beat"], "dist": 1 - d["match"], "overlap": d["vocal_overlap"]} for d in sug["donors"]]
                b0, b1 = self.beats[sug["beat"]], self.beats[sug["beat"] + sug["beats"]]
                lags = [refine_lag(self.stem_env[sug["stem"]], b0, b1, self.beats[d["beat"]]) for d in donors]
                res = repair_window(self.stems[sug["stem"]], self.stems["vocals"], self.inst_sum,
                                    int(sug["start"] * self.sr), int(sug["end"] * self.sr), lags, max(sug["conf"], 0.5),
                                    strength, self.B, self.centers, self.sr, self._level_floor(sug["stem"]), mode="replace")
                self.patches[sid] = (res["lo"], res["patch"].astype(np.float16))
            elif sug["type"] == "ess":
                s0, s1 = self._window_samples(sug["beat"], sug["beats"])
                e = ess_window(self.stems[sug["stem"]], self.stems["vocals"], s0, s1, self.sr, strength)
                if e is not None:
                    self.patches[sid] = (e["lo"], e["patch"].astype(np.float16))
            elif sug["type"] == "smooth":
                s0, s1 = int(sug["start"] * self.sr), int(sug["end"] * self.sr)
                res = smooth_window(self.stems[sug["stem"]], self.stems["vocals"], self.inst_sum, s0, s1, strength)
                self.patches[sid] = (res["lo"], res["patch"].astype(np.float16))
            elif sug["type"] == "bleed" and "beat" in sug:
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

    # ------------------------------------------------------------------ best versions of repeating parts
    def _bars_in(self, sec):
        return [i for i, (s0, L) in enumerate(self.windows)
                if sec["start"] - 1e-3 <= self.beat_times[s0] < sec["end"] - 1e-3]

    def _instance(self, k, si):
        """How clean one section's take of one instrument is: the share of its sound the vocals covered."""
        F, C, R = self.stem_feats[k]
        beats = [b for i in self._bars_in(self.sections[si])
                 for b in range(self.windows[i][0], self.windows[i][0] + self.windows[i][1])]
        if not beats:
            return None
        E = np.exp(F[beats])
        level = float(10 * np.log10(E.sum(1).mean() + EPS))
        # how much of it the vocals took: the detected loss here, or the share the vocals covered
        lost = self.grid.get(f"{si}:{k}", {}).get("damage", 0.0) if hasattr(self, "grid") else 0.0
        cover = float(max(3 * lost, (E * R[beats]).sum() / (E.sum() + EPS)))
        glitches = sum(1 for x in self.suggestions.values() if x["type"] == "glitch" and x["stem"] == k
                       and x["section"] == si and x["status"] == "pending")
        return {"cover": cover, "quality": float(np.clip(1 - cover - 0.05 * glitches, 0, 1)), "level": level}

    def _bar_dist(self, k, w1, w2):
        """Same yardstick as repair matching: timbre and harmony where neither bar had vocals."""
        F, C, R = self.stem_feats[k]
        (s1, L1), (s2, L2) = self.windows[w1], self.windows[w2]
        if L1 != L2:
            return None
        F1, F2, R1, R2 = F[s1:s1 + L1], F[s2:s2 + L2], R[s1:s1 + L1], R[s2:s2 + L2]
        w = (1 - R1) * (1 - R2)
        bd = float((w * ((F1 - F1.mean()) - (F2 - F2.mean())) ** 2).sum() / (w.sum() + EPS))
        cd = float(1 - (C[s1:s1 + L1] * C[s2:s2 + L2]).sum(1).mean())
        return bd, cd

    def _dist_scale(self, k):
        cache = self.__dict__.setdefault("_dscale", {})
        if k not in cache:
            rng = np.random.default_rng(0)
            n = len(self.windows)
            bds, cds = [], []
            for _ in range(200):
                d = self._bar_dist(k, int(rng.integers(n)), int(rng.integers(n)))
                if d:
                    bds.append(d[0]); cds.append(d[1])
            cache[k] = (float(np.median(bds) or 1) + EPS, float(np.median(cds) or 1) + EPS)
        return cache[k]

    def section_alignment(self, k, si, sj):
        """(similarity 0..1, bar offset): how alike the instrument's part is in two sections, bar by bar,
        trying each bar offset so a section boundary found a bar early or late doesn't matter."""
        bi, bj = self._bars_in(self.sections[si]), self._bars_in(self.sections[sj])
        if not bi or not bj:
            return 0.0, 0
        mb, mc = self._dist_scale(k)
        best = (0.0, 0)
        for off in range(min(len(bj), 8)):
            ds = []
            for n_, w1 in enumerate(bi):
                d = self._bar_dist(k, w1, bj[(n_ + off) % len(bj)])
                if d:
                    ds.append(0.5 * d[0] / mb + 0.5 * d[1] / mc)
            if ds:
                sim = float(np.clip(1.3 * (1 - np.mean(ds)), 0, 1))
                if sim > best[0]:
                    best = (sim, off)
        return best

    def section_similarity(self, k, si, sj):
        return self.section_alignment(k, si, sj)[0]

    def best_versions(self):
        """For each instrument in each sung section: a cleaner section where it plays the same part."""
        if self.stage != "detected" or not self.sections:
            return []
        out = []
        for k in [x for x in self.instruments if x not in NOT_REPAIRED]:
            info = {si: self._instance(k, si) for si in range(len(self.sections))}
            for ti, sec in enumerate(self.sections):
                it = info[ti]
                if not it or not sec.get("vocal") or it["cover"] < 0.03:
                    continue
                cands = []
                for si in range(len(self.sections)):
                    i_s = info[si]
                    if si == ti or not i_s or i_s["quality"] < it["quality"] + 0.03 or i_s["level"] < it["level"] - 15:
                        continue
                    sim = self.section_similarity(k, ti, si)
                    bt_, bs_ = self._bars_in(self.sections[ti]), self._bars_in(self.sections[si])
                    chord_ok = (not bt_ or not bs_ or
                                np.mean([self.chords_match(a_, b_) for a_, b_ in zip(bt_, bs_)]) >= 0.8)
                    same_part = self.sections[ti].get("label") == self.sections[si].get("label")
                    if sim >= 0.85 and chord_ok:    # (was 0.7: alike-but-different parts got swapped in)
                        cands.append({"section": si, "name": self.sections[si]["name"], "similarity": sim,
                                      "cover": i_s["cover"], "quality": i_s["quality"], "same_part": same_part})
                if not cands:
                    continue
                # the same part of the song (Chorus 2 from Chorus 1) first, then the cleanest
                cands.sort(key=lambda c: (c["same_part"], round(c["quality"], 2), c["similarity"]), reverse=True)
                existing = self.suggestions.get(f"t_{k}_{ti}")
                out.append({"stem": k, "section": ti, "name": sec["name"], "cover": it["cover"], "quality": it["quality"],
                            "best": cands[0], "candidates": cands[:5],
                            "status": existing["status"] if existing else None,
                            "source": existing.get("source_section") if existing else None})
        return out

    def make_take(self, k, ti, si):
        """Offer to replace instrument k in section ti with its take from section si (bar by bar,
        beat-aligned, level-matched, cross-faded). Added for review; nothing is applied."""
        with self.lock:
            T, S = self.sections[ti], self.sections[si]
            bt, bs = self._bars_in(T), self._bars_in(S)
            if not bt or not bs:
                raise RuntimeError("Those sections have no complete bars to copy.")
            F, C, R = self.stem_feats[k]
            beats_t = [b for i in bt for b in range(self.windows[i][0], self.windows[i][0] + self.windows[i][1])]
            beats_s = [b for i in bs for b in range(self.windows[i][0], self.windows[i][0] + self.windows[i][1])]
            # level match on the frequency bands the vocals left alone in both
            rel_t, rel_s = (1 - R[beats_t]).mean(0), (1 - R[beats_s]).mean(0)
            wgt = rel_t * rel_s
            dlog = float((wgt * (F[beats_t].mean(0) - F[beats_s].mean(0))).sum() / (wgt.sum() + EPS))
            g = float(np.clip(np.exp(dlog / 2), 0.5, 2.0))
            src_audio = self.render_stem(k)
            raw = self.stems[k]
            n = raw.shape[1]
            a = max(0, int(T["start"] * self.sr) - XFADE)
            b = min(n, int(T["end"] * self.sr) + XFADE)
            acc = np.zeros((2, b - a), np.float32)
            W = np.zeros(b - a, np.float32)
            off = self.section_alignment(k, ti, si)[1]
            for j, wi in enumerate(bt):
                wj = bs[(j + off) % len(bs)]
                (st, Lt), (ss, Ls) = self.windows[wi], self.windows[wj]
                if Lt != Ls:
                    continue
                t0, t1 = self._window_samples(st, Lt)
                lag = refine_lag(self.stem_env[k], self.beats[st], self.beats[st + Lt], self.beats[ss])
                lo, hi = max(a, t0 - XFADE // 2), min(b, t1 + XFADE // 2)
                w = _window_weight(hi - lo, t0 - lo, t1 - lo)
                seg = segment(src_audio, lo + lag, hi + lag) * g
                acc[:, lo - a:hi - a] += seg * w[None, :]
                W[lo - a:hi - a] += w
            W = np.clip(W, 0, 1)
            patch = acc - raw[:, a:b] * W[None, :]
            sid = f"t_{k}_{ti}"
            self.suggestions[sid] = {
                "id": sid, "type": "take", "stem": k, "status": "pending", "start": T["start"], "end": T["end"],
                "section": ti, "source_section": si, "conf": self.section_similarity(k, ti, si), "severity": 0.0,
                "strength": 1.0, "gain_db": float(20 * np.log10(g)),
                "donors": [{"beat": self.windows[bs[0]][0], "start": S["start"], "end": S["end"], "section": si,
                            "match": self.section_similarity(k, ti, si), "vocal_overlap": 0.0}],
                "note": f"Plays the {k} from {S['name']} here instead, matched to the beat and level"}
            self.patches[sid] = (a, patch.astype(np.float16))
            self.take_w[sid] = (a, W.astype(np.float16))
            return sid

    # ------------------------------------------------------------------ best hits (kick, snare, hi-hat...)
    PERCUSSIVE = ("drums", "kick", "snare", "toms", "cymbals")

    def _sounds(self, k):
        cache = self.__dict__.setdefault("_sounds_cache", {})
        if k not in cache:
            x = self.stems[k].mean(0)
            pos, clusters, F, lv, centers = HT.find_sounds(x, self.sr)
            vc = self._vocal_curve(0, x.shape[0])
            cov = vc[np.clip(pos + int(0.02 * self.sr), 0, len(vc) - 1)] if len(pos) else np.zeros(0)
            sounds = []
            for ci, c in enumerate(clusters):
                clean = [i for i in c if cov[i] < 0.08]
                damaged = [i for i in c if cov[i] >= 0.3]
                if len(clean) < 2 or not damaged:
                    continue
                # most typical clean hit: closest to the clean hits' average shape, at a typical level
                centroid = F[clean].mean(0)
                med = np.median(lv[clean])
                score = [float(((F[i] - centroid) ** 2).mean() + 0.05 * abs(lv[i] - med)) for i in clean]
                ranked = [int(clean[j]) for j in np.argsort(score)]      # cleanest, most typical first
                nm = (HT.name_sound(F[c].mean(0), centers) if k in self.PERCUSSIVE
                      else f"repeated note {chr(65 + len(sounds) % 26)}")
                sounds.append({"id": ci, "name": nm, "count": len(c),
                               "clean": len(clean), "damaged": [int(i) for i in damaged], "best": ranked[0],
                               "members": [int(i) for i in c],
                               "ranked": ranked})
            cache[k] = {"pos": pos, "next": np.r_[pos[1:], x.shape[0]] if len(pos) else pos, "F": F, "lv": lv,
                        "sounds": sounds}
        return cache[k]

    def best_hits(self, lazy=False):
        if self.stage != "detected":
            return []
        out = []
        for k in self.instruments:
            if k not in self.PERCUSSIVE and not self._tonal(k):
                continue
            if lazy and k not in self.__dict__.get("_sounds_cache", {}):
                self._warm(("sounds", k), lambda k=k: self._sounds(k))
                continue
            try:
                sd = self._sounds(k)
            except Exception as e:
                log(f"note starts skipped for {k}: {e}")
                continue
            for snd in sd["sounds"]:
                t_best = float(sd["pos"][snd["best"]] / self.sr)
                secs = sorted({self._section_of(sd["pos"][i] / self.sr) for i in snd["damaged"]})
                existing = [x for x in self.suggestions.values() if x["type"] == "hits" and x["stem"] == k
                            and x.get("sound") == snd["id"] and not x.get("pattern")]
                # show the version that's in use (applied, else waiting, else rejected)
                rank = {"accepted": 0, "pending": 1}
                existing.sort(key=lambda x: rank.get(x["status"], 2))
                out.append({"stem": k, "sound": snd["id"], "name": snd["name"], "count": snd["count"],
                            "kind": "drum" if k in self.PERCUSSIVE else "note",
                            "damaged": len(snd["damaged"]), "best_time": t_best, "sections": secs,
                            "clean": snd["clean"], "blend_n": min(self.BLEND_HITS, len(snd.get("ranked", [snd["best"]]))),
                            "status": existing[0]["status"] if existing else None,
                            "blended": bool(existing and existing[0].get("blend"))})
        return out

    BLEND_HITS = 5      # how many of the cleanest hits a blend combines

    def _tonal(self, k):
        """Pitched instruments whose note starts can be rebuilt like drum hits (only the attack)."""
        return k not in self.PERCUSSIVE and k not in NOT_REPAIRED and k != "vocals" and k in self.stems

    def make_hits(self, k, sound_id, start=None, end=None, blend=False):
        """Offer to rebuild every vocal-damaged hit of one repeated sound, one review item per section.
        Nothing is applied until the person applies it.

        blend=False: fill in from the single cleanest example.
        blend=True:  fill in from the BLEND_HITS cleanest examples together. Each is lined up to the
                     damaged hit, level-matched, and the repair engine takes the per-frequency median of
                     them (a mean when there are only two), so a wash or a bit of bleed that's in only one
                     of them drops out. The phase comes from the cleanest hit, so the attack stays sharp
                     instead of smearing the way averaging the raw waveforms would."""
        with self.lock:
            sd = self._sounds(k)
            snd = next(x for x in sd["sounds"] if x["id"] == sound_id)
            raw = self.stems[k]
            n = raw.shape[1]
            pos, nxt = sd["pos"], sd["next"]
            pre = int(0.003 * self.sr)
            min_len = int(0.03 * self.sr)
            perc = k in self.PERCUSSIVE
            src_cap, hit_cap = (0.35, 0.25) if perc else (0.15, 0.12)   # notes: just the attack
            ranked = snd.get("ranked") or [snd["best"]]
            src = []       # (start sample, usable length) of each clean hit used, cleanest first
            for j in (ranked[:self.BLEND_HITS] if blend else ranked[:1]):
                p = int(pos[j])
                ln = int(min(nxt[j] - p, src_cap * self.sr))
                if p - pre >= 0 and ln >= min_len:
                    src.append((p, ln))
            if not src:
                return []
            bp = src[0][0]
            best_len = min(ln for _, ln in src)
            srcs = [raw[:, p - pre:p + best_len].mean(0) for p, _ in src]
            by_sec = {}
            for i in snd["damaged"]:
                t_i = pos[i] / self.sr
                if (start is not None and t_i < start) or (end is not None and t_i >= end):
                    continue
                by_sec.setdefault(self._section_of(pos[i] / self.sr), []).append(i)
            ids = []
            for si, hits_i in by_sec.items():
                sec = self.sections[si]
                a, b = max(0, int(sec["start"] * self.sr) - 2048), min(n, int(sec["end"] * self.sr) + int(0.4 * self.sr))
                patch = np.zeros((2, b - a), np.float32)
                used = 0
                for i in hits_i:
                    p0 = int(pos[i])
                    L = int(min(nxt[i] - p0, best_len, hit_cap * self.sr))
                    if L < int(0.03 * self.sr) or p0 - pre - MARGIN < a or p0 + L + MARGIN > b:
                        continue
                    # line the clean hit up with this one, sample-accurately, then fill in only what the
                    # vocals took (same engine as repairs, with the clean hit as the source)
                    tgt = raw[:, p0 - pre:p0 + L].mean(0)
                    lags = []
                    for (sp, _), s in zip(src, srcs):
                        if abs(sp - p0) < L + pre:
                            continue                  # never borrow a hit from on top of itself
                        sh = HT.align(s[:pre + L], tgt, int(0.002 * self.sr))
                        lags.append((sp - p0) - sh)
                    if not lags:
                        continue
                    res = repair_window(raw, self.stems["vocals"], self.inst_sum, p0 - pre, p0 + L, lags, 1.0, 1.0,
                                        self.B, self.centers, self.sr, self._level_floor(k))
                    if res["severity"] < 0.02:
                        continue                      # this hit wasn't really damaged
                    lo = res["lo"]
                    q = res["patch"]
                    o0, o1 = max(lo, a), min(lo + q.shape[1], b)
                    patch[:, o0 - a:o1 - a] += q[:, o0 - lo:o1 - lo]
                    used += 1
                if not used:
                    continue
                base = f"h_{k}_{sound_id}_{si}"
                tail = "" if start is None else f"_{int(start * 100)}"
                sid, other = (base + "_blend" + tail, base + tail) if blend else (base + tail, base + "_blend" + tail)
                # blend and single-hit versions of the same part are alternatives: switching to one
                # takes the other out (Undo brings it back)
                if self.suggestions.get(other, {}).get("status") in ("accepted", "pending"):
                    self.set_status([other], "rejected", f"Switched {snd['name']} hits to "
                                                         f"{'a blend' if blend else 'the cleanest hit'}")
                hs = "s" if used != 1 else ""
                how = (f"from a blend of the {len(src)} cleanest ones" if len(src) > 1
                       else f"from the cleanest one at {bp / self.sr:.1f}s")
                self.suggestions[sid] = {
                    "id": sid, "type": "hits", "stem": k, "sound": sound_id, "status": "pending", "blend": bool(blend),
                    "start": sec["start"], "end": sec["end"], "section": si, "conf": 1.0, "severity": 0.0,
                    "strength": 1.0,
                    "donors": [{"beat": 0, "start": p / self.sr, "end": (p + best_len) / self.sr,
                                "section": self._section_of(p / self.sr), "match": 1.0, "vocal_overlap": 0.0}
                               for p, _ in src],
                    "note": f"Rebuilds {used} {snd['name']} hit{hs} the vocals smeared, filling in what's missing {how}"}
                self.patches[sid] = (a, patch.astype(np.float16))
                ids.append(sid)
            return ids

    # ------------------------------------------------------------------ repeating samples of any length
    LOOP_SIM = 0.88            # how alike two copies must be to count as the same sample
    LOOP_LENGTHS = (1, 2, 4)          # a sample is at least a whole bar (single hits are exported separately)

    def _beat_chroma(self, y):
        """Pitch-class content (n_beats, 12) of a signal, per beat, 80 Hz to 5 kHz."""
        x = np.asarray(y, np.float32)
        x = x.mean(0) if x.ndim == 2 else x
        f = np.fft.rfftfreq(N_FFT, 1 / self.sr)
        ok = (f >= 80) & (f <= 5000)
        pc = np.round(12 * np.log2(np.where(ok, f, 440) / 440.0)).astype(int) % 12
        M = np.zeros((12, len(f)), np.float32)
        M[pc[ok], np.flatnonzero(ok)] = 1
        bt = np.asarray(self.beats, int)
        win = np.hanning(N_FFT).astype(np.float32)
        out = np.zeros((max(0, len(bt) - 1), 12), np.float32)
        for j in range(len(bt) - 1):
            a, b = bt[j] * HOP, bt[j + 1] * HOP
            fr = np.arange(a, max(a + 1, b - N_FFT), N_FFT // 2)[:8]
            seg = [x[t:t + N_FFT] for t in fr if t + N_FFT <= len(x)]
            if not seg:
                continue
            S = np.abs(np.fft.rfft(np.stack(seg) * win, axis=1)).mean(0)
            c = M @ S
            out[j] = c / (np.linalg.norm(c) + 1e-9)
        return out

    def _quarter_beat_feats(self, y):
        """Log band levels per quarter beat (n_beats*4, bands) of a (2, n) or mono signal."""
        x = np.asarray(y, np.float32)
        x = x.mean(0) if x.ndim == 2 else x
        E = np.log(VF.band_energy(x, self.B) + 1e-9)          # (bands, frames at HOP)
        bt = np.asarray(self.beats, float)
        if len(bt) < 2:
            return np.zeros((0, E.shape[0]), np.float32)
        q = np.concatenate([np.linspace(bt[j], bt[j + 1], 5)[:-1] for j in range(len(bt) - 1)] + [bt[-1:]])
        idx = np.clip(np.round(q).astype(int), 0, E.shape[1] - 1)
        out = np.stack([E[:, idx[i]:max(idx[i] + 1, idx[i + 1])].mean(1) for i in range(len(idx) - 1)])
        return out.astype(np.float32)

    def find_loops(self, y, min_count=3, limit=6, drums=False):
        """Repeating samples in `y` (an instrument or the whole instrumental), at the length they really repeat
        at: 1, 2 or 4 bars, always starting on the bar. A 2-bar loop whose two halves
        are the same is reported as a 1-bar loop, so the length is the true one, never a single hit.
        Returns [{"beats": (b0, b1) of the cleanest copy, "len_bars", "members": [(b0, b1)...] cleanest first,
        "count"}]."""
        if not len(self.windows):
            return []
        Q = self._quarter_beat_feats(y)
        Cb = None if drums else self._beat_chroma(y)
        full = max(L for _, L in self.windows)
        va = np.asarray(self.VA, float) if len(self.VA) else np.zeros(len(self.beats))
        lev = Q.mean(1)
        floor = np.percentile(lev, 60) - np.log(10 ** 1.5)   # 15 dB under the usual level counts as not playing

        def feat(b0, b1):
            X = Q[b0 * 4:b1 * 4]
            X = X - X.mean(0, keepdims=True)
            v = X.ravel()
            v = v / (np.linalg.norm(v) + 1e-9)
            if Cb is None:
                return v
            c = Cb[b0:b1].ravel()
            c = c - c.mean()
            c = c / (np.linalg.norm(c) + 1e-9)
            return np.concatenate([v, c]) / np.sqrt(2)          # rhythm and harmony count the same

        bar_starts = [s for s, L in self.windows if L == full]
        out, covered = [], set()
        for m in self.LOOP_LENGTHS:
            nb = int(round(m * full))
            if nb < 2:
                continue
            starts = []
            for s in bar_starts:
                starts.append(s)
            units = [(s, s + nb) for s in starts if s + nb < len(self.beats) and s + nb <= len(Q) // 4]
            units = [u for u in units if lev[u[0] * 4:u[1] * 4].mean() > floor]
            if len(units) < min_count:
                continue
            V = np.stack([feat(*u) for u in units])
            S = V @ V.T
            clean = np.array([va[u[0]:u[1]].mean() if u[1] <= len(va) else 1.0 for u in units])
            used = np.zeros(len(units), bool)
            for i in np.argsort(clean):
                if used[i]:
                    continue
                cand = [j for j in np.argsort(-S[i]) if S[i, j] >= self.LOOP_SIM and not used[j]]
                members = []
                for j in sorted(cand, key=lambda j: clean[j]):
                    if all(units[j][1] <= units[k][0] or units[j][0] >= units[k][1] for k in members):
                        members.append(j)
                need = 2 if m >= 4 else min_count
                if len(members) < need:
                    continue
                b0, b1 = units[members[0]]
                # the true length: if both halves of the cleanest copy are the same, a shorter loop covers it
                h = (b1 - b0) // 2
                if m > 1 and h >= 2 and float(feat(b0, b0 + h) @ feat(b0 + h, b1)) >= self.LOOP_SIM:
                    used[members] = True
                    continue
                if sum(1 for j in members if units[j][0] in covered) > len(members) // 2:
                    used[members] = True                    # already exported as part of a shorter loop's run
                    continue
                used[members] = True
                for j in members:
                    covered.update(range(units[j][0], units[j][1]))
                out.append({"beats": (b0, b1), "len_bars": m, "count": len(members),
                            "members": [units[j] for j in members]})
        out.sort(key=lambda g: -(g["count"] * g["len_bars"]))
        return out[:limit]

    def _loop_audio(self, y, g, merged, n_merge=5):
        """The cleanest copy of a loop, or a merge of its cleanest copies (each stretched to the same length)."""
        a, b = int(self.beats[g["beats"][0]] * HOP), int(self.beats[g["beats"][1]] * HOP)
        best = y[:, a:b]
        if not merged or len(g["members"]) < 2:
            return best
        clips = [best]
        for b0, b1 in g["members"][1:n_merge]:
            av, bv = int(self.beats[b0] * HOP), int(self.beats[b1] * HOP)
            c = y[:, av:bv]
            if c.shape[1] < 64:
                continue
            xi = np.linspace(0, c.shape[1] - 1, best.shape[1])
            clips.append(np.stack([np.interp(xi, np.arange(c.shape[1]), c[ch]) for ch in range(c.shape[0])]).astype(np.float32))
        return self._merge_clips(clips) if len(clips) >= 2 else best

    # ------------------------------------------------------------------ best bars (riffs and patterns that repeat)
    def _bar_cover(self, k, wi):
        F, C, R = self.stem_feats[k]
        s0, L = self.windows[wi]
        E = np.exp(F[s0:s0 + L])
        lost = 0.0
        for x in self.suggestions.values():
            if x["type"] == "repair" and x["stem"] == k and x.get("window") == wi:
                lost = x.get("severity", 0.0)
        return float(max(3 * lost, (E * R[s0:s0 + L]).sum() / (E.sum() + EPS))), float(np.log(E.sum(1).mean() + EPS))

    SWAP_SIM = 0.93          # how alike two bars/parts must be before one may replace the other
    CHORD_SIM = 0.88         # ...and the chords around them (all instruments) must match this closely

    def _bar_chords(self):
        """Pitch content of the whole instrumental per bar (unit vectors), for 'same chord here?' checks."""
        if "_bar_chords" not in self.__dict__:
            C = self._beat_chroma(self.inst_sum)
            out = []
            for s0, L in self.windows:
                v = C[s0:s0 + L].sum(0) if L else np.zeros(12)
                out.append(v / (np.linalg.norm(v) + 1e-9))
            self.__dict__["_bar_chords"] = np.array(out)
        return self.__dict__["_bar_chords"]

    def chords_match(self, w1, w2):
        try:
            C = self._bar_chords()
            return float(C[w1] @ C[w2]) >= self.CHORD_SIM
        except Exception:
            return True

    def best_bars(self, min_sim=None):
        """Groups of bars where an instrument plays the same thing, with a clean one to copy from."""
        if self.stage != "detected":
            return []
        min_sim = self.SWAP_SIM if min_sim is None else min_sim
        out = []
        for k in [x for x in self.instruments if x not in NOT_REPAIRED and x not in self.PERCUSSIVE]:
            mb, mc = self._dist_scale(k)
            nb = len(self.windows)
            info = [self._bar_cover(k, w) for w in range(nb)]
            loud = np.array([x[1] for x in info])
            playing = loud > np.percentile(loud, 90) - 6
            todo = [w for w in range(nb) if playing[w] and self.windows[w][1] == self.windows[0][1] or playing[w]]
            used = set()
            for w in sorted(todo, key=lambda w: info[w][0]):          # seed from the cleanest bars
                if w in used or info[w][0] > 0.08:
                    continue
                group = [w]
                for v in todo:
                    if v == w or v in used:
                        continue
                    d = self._bar_dist(k, w, v)
                    if d and 1.3 * (1 - (0.5 * d[0] / mb + 0.5 * d[1] / mc)) >= min_sim and self.chords_match(w, v):
                        group.append(v)
                damaged = [v for v in group if info[v][0] >= 0.1]
                if len(group) >= 3 and damaged:
                    used.update(group)
                    existing = self.suggestions.get(f"b_{k}_{w}")
                    out.append({"stem": k, "best": w, "best_time": self.beat_times[self.windows[w][0]],
                                "count": len(group), "damaged": damaged,
                                "members": sorted(group, key=lambda v: info[v][0]),
                                "status": existing["status"] if existing else None})
        return out

    def make_bars(self, k, best_w, damaged, members=None, blend=False):
        """Offer to replace the damaged repeats of a bar with its cleanest repeat (beat-aligned, level-matched).
        blend=True: with a merge of the cleanest repeats instead (per-frequency median, so what's in only one
        copy, like a trace of singing, drops out)."""
        with self.lock:
            raw = self.stems[k]
            n = raw.shape[1]
            src_audio = self.render_stem(k)
            F, C, R = self.stem_feats[k]
            sb, Lb = self.windows[best_w]
            clean_m = [v for v in (members or []) if v not in damaged and self.windows[v][1] == Lb]
            n_blend = 0
            if blend and len(clean_m) >= 2:
                ba, bb = self._window_samples(sb, Lb)
                g = {"beats": (sb, sb + Lb), "members": [(self.windows[v][0], self.windows[v][0] + Lb)
                                                         for v in [best_w] + [v for v in clean_m if v != best_w]]}
                ref = self._loop_audio(src_audio, g, True, self.BLEND_HITS)
                src_audio = src_audio.copy()
                src_audio[:, ba:ba + ref.shape[1]] = ref
                n_blend = min(self.BLEND_HITS, len(g["members"]))
            starts = [self._window_samples(*self.windows[v]) for v in damaged]
            a = max(0, min(x[0] for x in starts) - XFADE)
            b = min(n, max(x[1] for x in starts) + XFADE)
            acc = np.zeros((2, b - a), np.float32)
            W = np.zeros(b - a, np.float32)
            def form_source(v):
                """The same bar of the same kind of section (bar 5 of Chorus 2 -> bar 5 of Chorus 1), when that
                copy is clean; else the overall cleanest."""
                if n_blend:
                    return sb
                try:
                    sv = self.sections[self._section_of(self.beat_times[self.windows[v][0]])]
                    off = v - sv.get("start_bar", 0)
                    for m in clean_m:
                        sm = self.sections[self._section_of(self.beat_times[self.windows[m][0]])]
                        if sm is not sv and sm.get("label") == sv.get("label") and m - sm.get("start_bar", 0) == off:
                            return self.windows[m][0]
                except Exception:
                    pass
                return sb
            for v, (t0, t1) in zip(damaged, starts):
                st_, Lt = self.windows[v]
                if Lt != Lb:
                    continue
                sbv = form_source(v)
                rel = (1 - R[st_:st_ + Lt]) * (1 - R[sbv:sbv + Lb])
                dlog = float((rel * (F[st_:st_ + Lt] - F[sbv:sbv + Lb])).sum() / (rel.sum() + EPS))
                g = float(np.clip(np.exp(dlog / 2), 0.5, 2.0))
                lag = refine_lag(self.stem_env[k], self.beats[st_], self.beats[st_ + Lt], self.beats[sbv])
                lo, hi = max(a, t0 - XFADE // 2), min(b, t1 + XFADE // 2)
                w = _window_weight(hi - lo, t0 - lo, t1 - lo)
                acc[:, lo - a:hi - a] += segment(src_audio, lo + lag, hi + lag) * g * w[None, :]
                W[lo - a:hi - a] += w
            W = np.clip(W, 0, 1)
            sid = f"b_{k}_{best_w}"
            t_best = self.beat_times[sb]
            self.suggestions[sid] = {
                "id": sid, "type": "bars", "stem": k, "status": "pending",
                "start": starts[0][0] / self.sr, "end": starts[-1][1] / self.sr, "section": self._section_of(starts[0][0] / self.sr),
                "conf": 1.0, "severity": 0.0, "strength": 1.0, "bars": [int(v) for v in damaged], "blend": bool(n_blend),
                "members": [int(v) for v in (members or [])],
                "donors": [{"beat": int(sb), "start": t_best, "end": self.beat_times[sb + Lb], "section": self._section_of(t_best),
                            "match": 1.0, "vocal_overlap": 0.0}],
                "note": f"Replaces {len(damaged)} damaged repeat{'s' if len(damaged) != 1 else ''} of this {k} bar with "
                        + (f"a merge of its {n_blend} cleanest repeats" if n_blend else f"its cleanest repeat, at {t_best:.1f}s")}
            self.patches[sid] = (a, (acc - raw[:, a:b] * W[None, :]).astype(np.float16))
            self.take_w[sid] = (a, W.astype(np.float16))
            return sid

    # ------------------------------------------------------------------ rebuild a part the person points at
    def rebuild_range(self, start, end, stem=None, strength=0.8):
        """Rebuild [start, end) of one instrument (or every one) from the bars that match it best,
        whether or not there were vocals. One review item per instrument and bar; nothing applied."""
        if not self.stem_feats:
            raise RuntimeError("Run Find damage once first; rebuilding needs the matches it finds.")
        with self.lock:
            ids = []
            for k in ([stem] if stem else [x for x in self.instruments if x not in NOT_REPAIRED]):
                if k not in self.stem_feats:
                    continue
                F, C, R = self.stem_feats[k]
                for wi, (s0b, L) in enumerate(self.windows):
                    w0, w1 = self._window_samples(s0b, L)
                    if w1 / self.sr <= start or w0 / self.sr >= end:
                        continue
                    donors, conf = match_window(F, C, R, s0b, L, self.meta["settings"]["n_donors"])
                    if not donors:
                        continue
                    a0, a1 = max(w0, int(start * self.sr)), min(w1, int(end * self.sr))
                    if a1 - a0 < self.sr // 10:
                        continue
                    b0, b1 = self.beats[s0b], self.beats[s0b + L]
                    lags = [refine_lag(self.stem_env[k], b0, b1, self.beats[d["beat"]]) for d in donors]
                    res = repair_window(self.stems[k], self.stems["vocals"], self.inst_sum, a0, a1, lags,
                                        max(conf, 0.5), strength, self.B, self.centers, self.sr,
                                        self._level_floor(k), mode="replace")
                    sid = f"rb_{k}_{a0}"
                    self.suggestions[sid] = {
                        "id": sid, "type": "rebuild", "stem": k, "status": "pending", "start": a0 / self.sr,
                        "end": a1 / self.sr, "section": self._section_of(a0 / self.sr), "conf": float(conf),
                        "severity": 0.0, "strength": strength, "window": wi, "beat": s0b, "beats": L,
                        "donors": self._donor_info(donors, L),
                        "note": "Rebuilt from the bars where this instrument plays the same part"}
                    self.patches[sid] = (res["lo"], res["patch"].astype(np.float16))
                    ids.append(sid)
            return ids

    def find_ess(self, start=None, end=None):
        """Look for leftover "s"/"t" sounds of the singer in each instrument. Review items; nothing applied."""
        ids = []
        for k in [x for x in self.instruments if x not in NOT_REPAIRED]:
            for wi, (s0b, L) in enumerate(self.windows):
                if self.VA[s0b:s0b + L].max() < self.meta["settings"]["vocal_threshold"]:
                    continue
                a0, a1 = self._window_samples(s0b, L)
                if (start is not None and a1 / self.sr <= start) or (end is not None and a0 / self.sr >= end):
                    continue
                e = ess_window(self.stems[k], self.stems["vocals"], a0, a1, self.sr)
                if e is None:
                    continue
                sid = f"e_{k}_{wi}"
                if sid in self.suggestions and self.suggestions[sid]["status"] != "pending":
                    continue
                self.suggestions[sid] = {
                    "id": sid, "type": "ess", "stem": k, "status": "pending", "start": a0 / self.sr, "end": a1 / self.sr,
                    "section": self._section_of(a0 / self.sr), "conf": 1.0, "severity": 0.0, "strength": 1.0,
                    "window": wi, "beat": s0b, "beats": L, "donors": [], "removed_db": e["removed_db"],
                    "note": f"The singer's \"s\" and \"t\" sounds leaked into the {k} ({e['bursts']} spot{'s' if e['bursts'] != 1 else ''} in this bar)"}
                self.patches[sid] = (e["lo"], e["patch"].astype(np.float16))
                ids.append(sid)
        return ids

    # ------------------------------------------------------------------ all repeated parts in one go
    def apply_best(self, kind="all", accept=True, blend=False):
        """Create every best-version / best-hit / repeated-bar fix at once and, if accept, apply them all
        (one undo step). blend: rebuild drum hits from a blend of their cleanest examples. Returns the ids."""
        ids = []
        if kind in ("all", "versions"):
            for x in self.best_versions():
                if not x["status"]:
                    ids.append(self.make_take(x["stem"], x["section"], x["best"]["section"]))
        if kind in ("all", "hits"):
            for h in self.best_hits():
                # with blend, also switch hits already done the single-hit way
                if not h["status"] or (blend and not h["blended"] and h["status"] != "rejected"):
                    ids += self.make_hits(h["stem"], h["sound"], blend=blend)
        if kind in ("all", "bars"):
            for g in self.best_bars():
                if not g["status"] or (blend and g["status"] != "rejected"):
                    ids.append(self.make_bars(g["stem"], g["best"], g["damaged"], g["members"], blend=blend))
        self.check_ids(ids)
        good = [i for i in ids if not self.flagged(i)]          # ones that don't fit wait in Review
        if accept and good:
            self.set_status(good, "accepted", f"Applied {len(good)} repeated-part fix(es)")
        return ids

    # ------------------------------------------------------------------ clean up one part in one go
    def clean_up(self, start, end):
        """Everything that can tidy [start, end): click/gap scan, smoothing under the vocals, rebuilt
        drum hits and repeated bars that fall in it. All added for review; nothing applied."""
        ids = []
        before = set(self.suggestions)
        self.scan_glitches(start, end)
        ids += [i for i in self.suggestions if i not in before]
        ids += self.smooth_range(start, end, [k for k in self.instruments if k not in self.PERCUSSIVE and k not in NOT_REPAIRED])
        try:
            ids += self.find_ess(start, end)
        except Exception as e:
            log(f"ess check skipped: {e}")
        for h in self.best_hits():
            if any(start <= self.sections[si]["end"] and self.sections[si]["start"] <= end for si in h["sections"]):
                ids += self.make_hits(h["stem"], h["sound"], start, end)
        for g in self.best_bars():
            dmg = [v for v in g["damaged"] if start <= self.beat_times[self.windows[v][0]] < end]
            if dmg:
                ids.append(self.make_bars(g["stem"], g["best"], dmg))
        return ids

    # ------------------------------------------------------------------ other models for one part
    def alt_modes(self):
        d = os.path.join(self.root, "alt")
        return sorted(m for m in os.listdir(d) if os.path.isdir(os.path.join(d, m))) if os.path.isdir(d) else []

    def set_alt(self, mode, stems):
        d = os.path.join(self.root, "alt", mode)
        os.makedirs(d, exist_ok=True)
        for k, v in stems.items():
            np.save(os.path.join(d, k + ".npy"), v.astype(np.float32))
        self.__dict__.setdefault("_alt_cache", {})[mode] = stems

    def load_alt(self, mode):
        cache = self.__dict__.setdefault("_alt_cache", {})
        if mode not in cache:
            d = os.path.join(self.root, "alt", mode)
            cache[mode] = {f[:-4]: np.load(os.path.join(d, f), mmap_mode="r") for f in os.listdir(d) if f.endswith(".npy")}
        return cache[mode]

    def make_alt(self, mode, start, end, stem=None, label=None, blend=0.0):
        """Offer to use another model's split for [start, end): one instrument, or all of them (stem None).
        Added for review; nothing is applied."""
        with self.lock:
            alt = self.load_alt(mode)
            n = self.original.shape[1]
            a, b = max(0, int(start * self.sr)), min(n, int(end * self.sr))
            lo, hi = max(0, a - XFADE), min(n, b + XFADE)
            t = np.arange(lo, hi) / self.sr
            ramp = 0.02
            W = np.minimum(np.clip((t - start + ramp / 2) / ramp, 0, 1), np.clip((end + ramp / 2 - t) / ramp, 0, 1)).astype(np.float32)
            cur = [k for k in self.instruments if k not in NOT_REPAIRED]

            def alt_for(k):
                def take(name):
                    x = np.asarray(alt[name][:, lo:hi], np.float32)
                    return np.pad(x, ((0, 0), (0, (hi - lo) - x.shape[1])))
                if k in alt:
                    return take(k)
                if k == "other" and "other" in alt:   # the other model's guitar/piano belong in "other" here
                    return take("other") + sum((take(x) for x in ("guitar", "piano") if x in alt and x not in cur),
                                               np.zeros((2, hi - lo), np.float32))
                if stem is None and k in ("guitar", "piano") and "other" in alt:
                    return np.zeros((2, hi - lo), np.float32)   # the other model has them inside "other"
                return None

            targets = [stem] if stem else cur
            ids, skipped = [], []
            name = label or mode
            for k in targets:
                y = alt_for(k)
                if y is None:
                    skipped.append(k)
                    continue
                raw = self.stems[k][:, lo:hi]
                if blend:      # half and half: each model's artifacts are different, so they soften
                    now = self.render_stem(k)[:, lo:hi]
                    y = blend * y + (1 - blend) * now
                sid = f"a_{mode}_{k}_{int(start * 100)}" + ("_mix" if blend else "")
                self.suggestions[sid] = {
                    "id": sid, "type": "alt", "stem": k, "status": "pending", "start": float(start), "end": float(end),
                    "section": self._section_of(start), "conf": 1.0, "severity": 0.0, "strength": 1.0, "donors": [],
                    "alt_mode": mode, "blend": blend,
                    "note": (f"Blends the {k} from the \"{name}\" split half and half with the current one here" if blend
                             else f"Uses the {k} from the \"{name}\" split here instead")}
                self.patches[sid] = (lo, (W[None, :] * (y - raw)).astype(np.float16))
                self.take_w[sid] = (lo, W.astype(np.float16))
                ids.append(sid)
            if not ids:
                raise RuntimeError("That split doesn't have a matching track for " + ", ".join(skipped) + ".")
            return ids, skipped

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
            for key in ("stem_gain", "mute", "solo", "section_gain", "keep", "vocal_lift", "mute_ranges", "sides", "mud_cut",
                        "tone_eq", "seams", "eq_ranges"):
                if key in mixer:
                    self.mixer[key] = mixer[key]
            if "keep" in mixer:
                self.mixer["keep"] = self._clean_keep(self.mixer["keep"])
            if "sides" in mixer:
                dur = self.meta.get("duration", 0)
                self.mixer["sides"] = [{"start": max(0.0, float(r["start"])), "end": min(dur, float(r["end"])),
                                        "amount": min(max(float(r.get("amount", 1.0)), 0.0), 1.0)}
                                       for r in self.mixer["sides"] if float(r["end"]) - float(r["start"]) >= 0.05]
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
            auto = bool(r.get("auto"))
            if out and out[-1]["mode"] == mode and a <= out[-1]["end"] + 0.01:
                out[-1]["end"] = max(out[-1]["end"], b)
                if not auto:
                    out[-1].pop("auto", None)          # merged with one of yours: it's yours
            else:
                out.append({"start": a, "end": b, "mode": mode, **({"auto": True} if auto else {})})
        return out

    def undo(self):
        with self.lock:
            if not self.history:
                return None
            h = self.history.pop()
            keep_changed = h["mixer"].get("keep") != self.mixer.get("keep")
            drums_changed = h["mixer"].get("drum_edits") != self.mixer.get("drum_edits")
            self.mixer = h["mixer"]
            if "refs" in h and h["refs"] != (self.meta.get("refs") or {}):
                for k_ in set(h["refs"]) | set(self.meta.get("refs") or {}):
                    self.display.get("raw", {}).pop(f"ref:{k_}", None)
                    self._render_cache.pop(("stem", k_), None)
                self.meta["refs"] = h["refs"]
            if drums_changed:
                for k_ in self.instruments:
                    self._render_cache.pop(("stem", k_), None)
            if keep_changed:
                for k in self.instruments:
                    self._render_cache.pop(("stem", k), None)
                    self._refresh_after(k, 0, self.stems[k].shape[1])
            for it in h["items"]:
                if it["sug"] is None:          # made by that step: undo removes it again
                    self._invalidate(it.get("stem", ""), it["id"])
                    self.suggestions.pop(it["id"], None)
                    self.patches.pop(it["id"], None)
                    self.take_w.pop(it["id"], None)
                    continue
                self.suggestions[it["id"]] = it["sug"]
                if it["patch"] is not None:
                    self.patches[it["id"]] = it["patch"]
                if it.get("take_w") is not None:
                    self.take_w[it["id"]] = it["take_w"]
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
                    s["status"] = "pending" if s["type"] != "repair" else (
                        "pending" if s["conf"] >= st["review_conf"] else "low")
                    self._invalidate(s["stem"], i)
                self._compute_grid()
            return
        self.set_status(ids, status, f"{action} {len(ids)} edit(s)")

    # ------------------------------------------------------------------ export
    def _match_gain(self, mix):
        """Gain that brings the instrumental to the original song's level, measured only where nobody
        sings (there the two should sound the same). 1.0 when there's too little of that to measure."""
        n = min(mix.shape[1], self.original.shape[1])
        free = self._vocal_curve(0, n) < 0.02
        if free.sum() < 5 * self.sr:
            return 1.0
        o = float((self.original[:, :n][:, free].astype(np.float64) ** 2).mean())
        m = float((mix[:, :n][:, free].astype(np.float64) ** 2).mean())
        return float(np.clip(np.sqrt(o / (m + EPS)), 0.5, 4.0)) if m > 0 else 1.0

    def export(self, folder, fmt="wav", what=("mix", "stems", "raw", "vocals", "report"), bits=24, match_level=False,
               declick=False, lufs=None):
        """Write the chosen files. Every audio file gets the SAME gain, so the exported instruments still
        add up to the exported mix and keep the balance you heard (they used to be turned down one by one
        when they came near clipping). match_level: bring everything to the original song's level."""
        os.makedirs(folder, exist_ok=True)
        name = os.path.splitext(self.meta.get("name") or "song")[0]
        ext = "." + fmt
        b = 16 if (fmt == "wav" and bits == 16) else 32
        written = []

        jobs = []          # (file name, function that renders the audio): rendered twice, so nothing big is held
        if "mix" in what:
            jobs.append((f"{name} - instrumental (repaired)", lambda: self.mix("repaired")))
        if "raw" in what:
            jobs.append((f"{name} - instrumental (raw)", lambda: self.mix("raw", use_mixer=False)))
        if "stems" in what:
            for k in self.instruments:
                jobs.append((f"{name} - {k} (repaired)",
                             lambda k=k: self.shaped_stem(k) * self._gain_curve(k, 0, self.stems[k].shape[1])))
        if "vocals" in what:
            jobs.append((f"{name} - vocals", lambda: self.stems["vocals"]))

        g = 1.0
        if match_level and self.original is not None:
            g = self._match_gain(self.mix("repaired"))
        self.meta.pop("last_export_lufs", None)
        if lufs is not None:
            # streaming loudness, measured on the instrumental; every file gets the same gain. Never louder than
            # -1 dB peak: a dynamic song may stay a little under the target rather than clip
            from .loudness import lufs as measure
            now = measure(self.mix("repaired"), self.sr)
            if np.isfinite(now):
                g = 10 ** ((float(lufs) - now) / 20)
        env = None
        if lufs is not None:
            # a gentle limiter (at most 6 dB, 5 ms look-ahead, 120 ms release), worked out on the instrumental and
            # applied to every file the same way, so the stems still add up to the mix
            from scipy.ndimage import minimum_filter1d, uniform_filter1d
            pk = np.abs(self.mix("repaired")).max(0) * g
            need = np.minimum(1.0, 0.891 / np.maximum(pk, 1e-9))
            if float(need.min()) < 1.0:
                floor = 10 ** (-6 / 20)
                if float(need.min()) < floor:
                    g *= float(need.min()) / floor        # more than 6 dB would pump: settle a little quieter
                    need = np.minimum(1.0, 0.891 / np.maximum(pk * float(need.min()) / floor, 1e-9))
                env = minimum_filter1d(need, int(0.005 * self.sr) * 2 + 1)
                env = np.minimum(env, uniform_filter1d(env, int(0.12 * self.sr)))
                env = minimum_filter1d(env, int(0.005 * self.sr) * 2 + 1).astype(np.float32)
        lim_audio = (lambda y: y * env[None, :y.shape[1]]) if env is not None else (lambda y: y)
        peak = max([float(np.abs(lim_audio(f() * g)).max()) for _, f in jobs] or [0.0])
        lim = 0.891 if lufs is not None else 0.999
        if peak > lim:
            g *= lim / peak                       # one shared trim keeps every file in step
        if lufs is not None:
            from .loudness import lufs as measure
            self.meta["last_export_lufs"] = round(measure(lim_audio(self.mix("repaired") * g), self.sr), 1)
        for fname, f in jobs:
            p = os.path.join(folder, fname + ext)
            audio = lim_audio(f() * g).astype(np.float32)
            if declick:                       # vinyl rips: clicks and crackle out of each file
                from . import vinyl as VN
                audio = VN.declick(audio, self.sr, "normal")[0]
            save_audio(p, audio, self.sr, bits=b)
            written.append(p)
        self.meta["last_export_gain_db"] = float(20 * np.log10(g + EPS))
        if "tempo" in what:
            written += self.export_tempo(folder, name)
        if "drum_midi" in what:
            p = self.export_drum_midi(os.path.join(folder, f"{name} - drums.mid"))
            if p:
                written.append(p)
        if "samples_raw" in what or "samples_merged" in what:
            written += self.export_samples(os.path.join(folder, f"{name} - samples"), raw="samples_raw" in what,
                                           merged="samples_merged" in what, fmt=fmt if fmt in ("wav", "flac") else "wav")
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
    @staticmethod
    def _sig(d):
        """Cheap fingerprint of a dict of arrays (or (offset, array) pairs): changes whenever an entry is
        added, removed, replaced or edited, without hashing hundreds of MB."""
        parts = []
        for key in sorted(d):
            v = d[key]
            lo, a = v if isinstance(v, tuple) else (0, v)
            a = np.asarray(a)
            flat = a.reshape(-1)
            sample = float(np.asarray(flat[::4093], np.float64).sum()) if flat.size else 0.0
            parts.append((key, int(lo), a.shape, str(a.dtype), sample))
        return hash(tuple(parts))

    def _heavy_parts(self):
        patches = {**{f"{k}__lo": (0, np.array([v[0]])) for k, v in self.patches.items()},
                   **{f"{k}__p": v[1] for k, v in self.patches.items()},
                   **{f"{k}__w": v[1] for k, v in self.take_w.items()},
                   **{f"{k}__wlo": (0, np.array([v[0]])) for k, v in self.take_w.items()}}
        display = ({f"{kind}__{k}": v for kind in ("raw", "after", "damage") for k, v in self.display.get(kind, {}).items()}
                   if self.display else None)
        feats = ({**{f"{k}__{i}": a for k, t in self.stem_feats.items() for i, a in enumerate(t)},
                  **{f"{k}__env": v for k, v in self.stem_env.items()}} if hasattr(self, "stem_feats") and self.display else None)
        return {"patches": patches, "display": display, "features": feats}

    def save(self):
        """Write the project. Only the parts that changed are written (the repairs file, timeline and
        analysis can be hundreds of MB, and this runs after every edit), and the song is only locked
        while the state is copied, not while the disk is written."""
        with self._save_lock:
            with self.lock:
                os.makedirs(self.root, exist_ok=True)
                state = json.dumps({"meta": self.meta, "sections": getattr(self, "sections", []),
                                    "beats": getattr(self, "beats", np.zeros(0)).tolist(),
                                    "windows": getattr(self, "windows", []), "VA": getattr(self, "VA", np.zeros(0)).tolist(),
                                    "suggestions": self.suggestions, "mixer": self.mixer})
                parts = self._heavy_parts()
                # copies of the dicts (the arrays themselves are replaced, never edited, so references are safe)
                parts = {name: (dict(d) if d is not None else None) for name, d in parts.items()}
            tmp = os.path.join(self.root, "project.json.tmp")
            with open(tmp, "w") as f:
                f.write(state)
            os.replace(tmp, os.path.join(self.root, "project.json"))
            # a tiny summary for the Songs list, so listing songs never reads the big project file
            summary = json.dumps({"name": self.meta.get("name"), "duration": self.meta.get("duration")})
            with open(os.path.join(self.root, "summary.json.tmp"), "w") as f:
                f.write(summary)
            os.replace(os.path.join(self.root, "summary.json.tmp"), os.path.join(self.root, "summary.json"))
            for name, d in parts.items():
                if d is None:
                    continue
                sig = self._sig(d)
                if self._saved.get(name) == sig:
                    continue
                arrays = {k: (v[1] if isinstance(v, tuple) else v) for k, v in d.items()}
                if name == "display":
                    arrays = {k: np.asarray(v).astype(np.float16) for k, v in arrays.items()}
                tmpz = os.path.join(self.root, f"{name}.tmp.npz")
                np.savez(tmpz, **arrays)
                os.replace(tmpz, os.path.join(self.root, f"{name}.npz"))
                self._saved[name] = sig

    @classmethod
    def load(cls, root):
        p = cls(root)
        with open(os.path.join(root, "project.json")) as f:
            state = json.load(f)
        p.meta, p.sections, p.suggestions, p.mixer = state["meta"], state["sections"], state["suggestions"], state["mixer"]
        p.mixer.setdefault("keep", [])
        p.mixer.setdefault("vocal_lift", {})
        p.mixer.setdefault("mud_cut", {})
        p.mixer.setdefault("tone_eq", {})
        p.mixer.setdefault("seams", [])
        p.mixer.setdefault("eq_ranges", [])
        p.mixer.setdefault("mute_ranges", [])
        p.mixer.setdefault("sides", [])
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
            elif key.endswith("__w"):
                sid = key[:-3]
                p.take_w[sid] = (int(z[sid + "__wlo"][0]), z[key])
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
        try:
            p._saved = {name: p._sig(d) for name, d in p._heavy_parts().items() if d is not None}
        except Exception as e:
            log(f"couldn't fingerprint saved files (they'll be rewritten once): {e}")
        return p

    # ------------------------------------------------------------------ UI payload
    def spectro(self, k, kind):
        """uint8 image bytes (rows = DISPLAY_BANDS low->high, row-major) for the timeline."""
        ref = self.display.get("ref_db", 0.0)
        if k.startswith("ref:"):                       # your clean track's own lane
            kk = k[4:]
            if k not in self.display["raw"]:
                self.display["raw"][k] = self._display_spec(self.ref_audio(kk))
            return _to_u8(self.display["raw"][k], ref - 85, ref).tobytes()
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
            if "guard_before" in values or "guard_after" in values:
                self.__dict__.pop("_guard", None)
            for key in ("auto_conf", "review_conf", "vocal_threshold", "min_severity", "guard_before", "guard_after"):
                if key in values:
                    st[key] = float(values[key])
            if ("guard_before" in values or "guard_after" in values) and self.mixer.get("keep"):
                for k in self.instruments:     # repairs are blocked only where the original is really used
                    self._render_cache.pop(("stem", k), None)
                    self._refresh_after(k, 0, self.stems[k].shape[1])
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

    # ------------------------------------------------------------------ learn the drum pattern
    SLOTS_PER_BEAT = 4          # a 16th-note grid
    PATTERN_SURE = 0.9          # the pattern must have this sound in this slot this often to act on it

    def _slot_of(self, t):
        """(bar index, slot in the bar, distance in beats) for time t, on the 16th-note grid."""
        bt = self.beat_times
        j = int(np.searchsorted(bt, t, side="right")) - 1
        if j < 0 or j >= len(bt) - 1:
            return None
        frac = (t - bt[j]) / max(bt[j + 1] - bt[j], 1e-6)
        sub = int(round(frac * self.SLOTS_PER_BEAT))
        off = abs(frac * self.SLOTS_PER_BEAT - sub) / self.SLOTS_PER_BEAT
        if sub == self.SLOTS_PER_BEAT:
            j, sub = j + 1, 0
        for wi, (s0, L) in enumerate(self.windows):
            if s0 <= j < s0 + L:
                return wi, (j - s0) * self.SLOTS_PER_BEAT + sub, off
        return None

    def _slot_time(self, wi, slot):
        s0, L = self.windows[wi]
        j = s0 + slot // self.SLOTS_PER_BEAT
        if j + 1 >= len(self.beat_times):
            return None
        sub = slot % self.SLOTS_PER_BEAT
        return self.beat_times[j] + (self.beat_times[j + 1] - self.beat_times[j]) * sub / self.SLOTS_PER_BEAT

    @staticmethod
    def _section_kind(name):
        import re as _re
        return _re.sub(r"[\s\d]+$", "", name or "").strip().lower() or "part"

    def learn_patterns(self):
        """For every repeated drum sound: how often it plays in each 16th-note slot of the bar, learned from
        the bars where nobody sings, per kind of section (verses, choruses...) and for the whole song."""
        out = {}
        bar_len = max((L for _, L in self.windows), default=4)
        va = np.asarray(self.VA)
        bar_sec = {}
        for wi, (s0, L) in enumerate(self.windows):
            t = self.beat_times[s0] if s0 < len(self.beat_times) else 0
            si = self._section_of(t)
            bar_sec[wi] = (si, self._section_kind(self.sections[si]["name"]) if si is not None and si < len(self.sections) else "part")
        for k in [x for x in self.instruments if x in self.PERCUSSIVE or x == "bass"]:
            try:
                sd = self._sounds(k)
            except Exception as e:
                log(f"pattern: {k} skipped: {e}")
                continue
            for snd in sd["sounds"]:
                slots_by_bar = {}
                for i in snd.get("members", []):
                    so = self._slot_of(sd["pos"][i] / self.sr)
                    if so and so[2] < 0.25 / self.SLOTS_PER_BEAT:       # close to the grid
                        slots_by_bar.setdefault(so[0], set()).add(so[1])
                # reference bars: full-length, nobody singing, this sound plays at least once
                ref = [wi for wi, (s0, L) in enumerate(self.windows)
                       if L == bar_len and wi in slots_by_bar and float(va[s0:s0 + L].mean() if L else 1) < 0.1]
                if len(ref) < 4:
                    continue
                def probs(bars):
                    c = np.zeros(bar_len * self.SLOTS_PER_BEAT)
                    for wi in bars:
                        for sl in slots_by_bar[wi]:
                            if sl < len(c):
                                c[sl] += 1
                    return c / max(1, len(bars))
                by_kind = {}
                for kind in {bar_sec[w][1] for w in ref}:
                    bars = [w for w in ref if bar_sec[w][1] == kind]
                    if len(bars) >= 4:
                        by_kind[kind] = probs(bars).tolist()
                out[(k, snd["id"])] = {"song": probs(ref).tolist(), "kinds": by_kind, "bars": slots_by_bar,
                                       "name": snd["name"], "bar_sec": bar_sec, "bar_len": bar_len}
        return out

    def _evidence(self, p, shape, band_w, sr):
        """Does the original recording have an attack at sample p with this sound's profile? Returns the
        exact attack position (sample) or None."""
        B, _ = HT._bands(sr)
        n = int(0.06 * sr)
        x = self.original.mean(0)
        search = int(0.02 * sr)
        if p - search - n < 0 or p + search + n >= len(x):
            return None
        hop = max(32, int(0.002 * sr))
        best, best_q = None, 0.0
        win = np.hanning(2048).astype(np.float32)
        def bands(a):
            seg = x[a:a + 2048]
            if len(seg) < 2048:
                return None
            return B @ (np.abs(np.fft.rfft(seg * win)) ** 2)
        for q in range(p - search, p + search, hop):
            eb, ea = bands(q - 2048), bands(q)
            if eb is None or ea is None:
                continue
            inc = np.maximum(ea - eb, 0)
            strength = float((inc * band_w).sum() / ((eb * band_w).sum() + 1e-12))
            if strength > best_q:
                best_q, best = strength, (q, inc)
        if best is None or best_q < 1.0:            # the sound's bands must at least double
            return None
        q, inc = best
        z = np.log(inc + 1e-12)
        z = z - z.mean()
        sh = shape - shape.mean()
        cos = float(z @ sh / (np.linalg.norm(z) * np.linalg.norm(sh) + 1e-12))
        return q if cos >= 0.6 else None

    def find_pattern_hits(self):
        """Hits the learned pattern is sure belong under the singing, that the drum track is missing and that
        the original recording does have there. Returns {(stem, sound): [sample positions]}."""
        if self.stage != "detected" or self.original is None:
            return {}
        pats = self.learn_patterns()
        va = np.asarray(self.VA)
        found = {}
        for (k, sid), P in pats.items():
            sd = self._sounds(k)
            snd = next(x for x in sd["sounds"] if x["id"] == sid)
            shape = sd["F"][snd["members"]].mean(0) if snd.get("members") else sd["F"][snd["best"]]
            w = np.exp(shape - shape.max())
            band_w = w / (w.sum() + 1e-12)
            lv_ref = float(np.median(sd["lv"][[i for i in snd["members"] if i not in snd["damaged"]] or snd["members"]]))
            x = self.stems[k].mean(0)
            B, _ = HT._bands(self.sr)
            have = {so[:2] for so in (self._slot_of(t / self.sr) for t in sd["pos"]) if so}
            hits = []
            for wi, (s0, L) in enumerate(self.windows):
                if L != P["bar_len"] or float(va[s0:s0 + L].mean() if L else 0) < 0.3:
                    continue                                  # only bars under the singing
                kind = P["bar_sec"][wi][1]
                prob = P["kinds"].get(kind, P["song"])
                for sl, pr in enumerate(prob):
                    if pr < self.PATTERN_SURE or (wi, sl) in have:
                        continue
                    t = self._slot_time(wi, sl)
                    if t is None:
                        continue
                    p = int(t * self.sr)
                    # the drum track is clearly missing it (well under its usual level for this sound)
                    _, lvs = HT._hit_features(x, np.array([p]), self.sr, B)
                    if float(lvs[0]) > lv_ref - np.log(4):        # within ~6 dB: it's there already
                        continue
                    q = self._evidence(p, shape, band_w, self.sr)
                    if q is not None:
                        hits.append(q)
            if hits:
                found[(k, sid)] = sorted(set(hits))
        return found

    def make_pattern_hits(self):
        """Restore the hits find_pattern_hits is confident about, from a blend of that sound's cleanest
        hits lined up to the attack in the original; only what the vocals took is filled in. One review
        item per drum sound and section. Returns the new ids."""
        with self.lock:
            found = self.find_pattern_hits()
            ids = []
            pre = int(0.003 * self.sr)
            for (k, sid), positions in found.items():
                sd = self._sounds(k)
                snd = next(x for x in sd["sounds"] if x["id"] == sid)
                raw = self.stems[k]
                n = raw.shape[1]
                src = []
                cap = 0.5 if k == "bass" else 0.25          # a bass note: its attack and the start of its body
                for j in (snd.get("ranked") or [snd["best"]])[:self.BLEND_HITS]:
                    pj = int(sd["pos"][j])
                    ln = int(min(sd["next"][j] - pj, cap * self.sr))
                    if pj - pre >= 0 and ln >= int(0.03 * self.sr):
                        src.append((pj, ln))
                if not src:
                    continue
                L = min(ln for _, ln in src)
                by_sec = {}
                for q in positions:
                    by_sec.setdefault(self._section_of(q / self.sr), []).append(q)
                for si, qs in by_sec.items():
                    if si is None:
                        continue
                    sec = self.sections[si]
                    a, b = max(0, int(sec["start"] * self.sr) - 2048), min(n, int(sec["end"] * self.sr) + int(0.4 * self.sr))
                    patch = np.zeros((2, b - a), np.float32)
                    used = 0
                    for q in qs:
                        if q - pre - MARGIN < a or q + L + MARGIN > b:
                            continue
                        tgt = self.original[:, q - pre:q + L].mean(0)        # line up with the attack in the original
                        lags = []
                        for sp, _ in src:
                            if abs(sp - q) < L + pre:
                                continue
                            sh = HT.align(raw[:, sp - pre:sp + L].mean(0), tgt, int(0.002 * self.sr))
                            lags.append((sp - q) - sh)
                        if not lags:
                            continue
                        res = repair_window(raw, self.stems["vocals"], self.inst_sum, q - pre, q + L, lags, 1.0, 1.0,
                                            self.B, self.centers, self.sr, self._level_floor(k))
                        if res["severity"] < 0.02:
                            continue
                        lo, pp = res["lo"], res["patch"]
                        o0, o1 = max(lo, a), min(lo + pp.shape[1], b)
                        patch[:, o0 - a:o1 - a] += pp[:, o0 - lo:o1 - lo]
                        used += 1
                    if not used:
                        continue
                    rid = f"p_{k}_{sid}_{si}"
                    self.suggestions[rid] = {
                        "id": rid, "type": "hits", "stem": k, "sound": sid, "status": "pending", "pattern": True,
                        "start": sec["start"], "end": sec["end"], "section": si, "conf": 1.0, "severity": 0.0,
                        "strength": 1.0, "blend": len(src) > 1,
                        "donors": [{"beat": 0, "start": pj / self.sr, "end": (pj + L) / self.sr,
                                    "section": self._section_of(pj / self.sr), "match": 1.0, "vocal_overlap": 0.0} for pj, _ in src],
                        "note": f"Restores {used} {snd['name']} {'note' if k == 'bass' else 'hit'}{'s' if used != 1 else ''} the "
                                f"{'bass' if k == 'bass' else 'drum'} pattern says belong here "
                                f"(the original recording has them; the vocals took them out of the drum track), "
                                f"from a blend of the {len(src)} cleanest"}
                    self.patches[rid] = (a, patch.astype(np.float16))
                    ids.append(rid)
            self.check_ids(ids)
            return ids

    # ------------------------------------------------------------------ auto-improve weak sections
    def _seg_score(self, y, a, b):
        """Badness of one stretch of one instrument against the original (lower is cleaner) and its level."""
        E = VF.band_energy(np.asarray(y, np.float32).mean(0), self.B)
        Eo = VF.band_energy(self.original[:, a:b].mean(0), self.B)
        Ev = VF.band_energy(self.stems["vocals"][:, a:b].mean(0), self.B)
        nf = min(E.shape[1], Eo.shape[1], Ev.shape[1])
        vc = self._vocal_curve(a, a + nf * HOP)[::HOP][:nf]
        vf = np.pad(vc, (0, nf - len(vc))) > 0.3
        vb = (np.asarray(self.centers) >= 250) & (np.asarray(self.centers) <= 5000)
        u, bl = VF.clean_terms(E[:, :nf], Eo[:, :nf], Ev[:, :nf], vf, vb)
        return 0.8 * u + 0.7 * max(0.0, bl - 0.25), 10 * np.log10(float(E.sum()) + 1e-12)

    def _with_candidate(self, sid, a, b, R, raw):
        """What the instrument would sound like over [a, b) with this (not yet applied) edit."""
        lo, p = self.patches[sid]
        P = np.zeros_like(R)
        o0, o1 = max(a, lo), min(b, lo + p.shape[1])
        if o1 > o0:
            P[:, o0 - a:o1 - a] = p[:, o0 - lo:o1 - lo].astype(np.float32)
        if sid in self.take_w:                        # replacing edit
            wlo, w = self.take_w[sid]
            W = np.zeros(b - a, np.float32)
            o0, o1 = max(a, wlo), min(b, wlo + len(w))
            if o1 > o0:
                W[o0 - a:o1 - a] = w[o0 - wlo:o1 - wlo].astype(np.float32)
            return R - (R - raw) * W[None, :] + P
        return R + P

    def improve_weak(self, threshold=75, progress=lambda f, m: None):
        """For every section and instrument scoring under `threshold`, try the alternatives (each other
        model's split, a half-and-half blend with it, other source parts for a best version), score each
        against the original, and apply the one that's clearly cleaner and still fits the original.
        Unchosen tries are removed. Returns report lines."""
        q = self.quality()
        cells = sorted(((v, key) for key, v in q["cells"].items() if v < threshold))
        pre = set(self.suggestions)            # (no weak sections still goes on to the weakest single bars)
        lines, n_done = [], 0
        for idx, (score, key) in enumerate(cells):
            si, k = key.split(":", 1)
            si = int(si)
            if k in NOT_REPAIRED or k not in self.instruments or si >= len(self.sections):
                continue
            sec = self.sections[si]
            if not sec.get("vocal"):
                continue
            progress(idx / max(1, len(cells)), f"Trying alternatives for the {k} in {sec['name']}")
            a, b = int(sec["start"] * self.sr), min(int(sec["end"] * self.sr), self.stems[k].shape[1])
            if b - a < self.sr:
                continue
            R = self.render_stem(k)[:, a:b]
            raw = self.stems[k][:, a:b]
            base, lv0 = self._seg_score(R, a, b)
            tries = []                                   # (score, sid, what)

            def score_of(sid):
                sc, lv = self._seg_score(self._with_candidate(sid, a, b, R, raw), a, b)
                return sc + 0.5 * float(np.clip((lv0 - lv) / 10, 0, 1))   # don't win by losing the part

            for mode in self.alt_modes():
                for bl in (0.0, 0.5):
                    try:
                        ids, _ = self.make_alt(mode, sec["start"], sec["end"], stem=k, blend=bl)
                        for sid in ids:
                            tries.append((score_of(sid), sid, f"the {mode} split{' blended half and half' if bl else ''}"))
                    except Exception as e:
                        log(f"improve: {mode} skipped for {k}: {e}")
            tid = f"t_{k}_{si}"
            if tid not in self.suggestions:          # never overwrite a best version that's already there
                bv = next((x for x in self.best_versions() if x["stem"] == k and x["section"] == si), None)
                best_src = None
                for c in (bv or {}).get("candidates", [])[:3]:
                    try:
                        self.make_take(k, si, c["section"])
                        sc = score_of(tid)
                    except Exception as e:
                        log(f"improve: source part skipped: {e}")
                        continue
                    if best_src is None or sc < best_src[0]:
                        best_src = (sc, c["section"])
                if best_src:
                    self.make_take(k, si, best_src[1])     # the best source part is the one kept
                    tries.append((best_src[0], tid, f"the same part from {self.sections[best_src[1]]['name']}"))
                elif tid in self.suggestions and tid not in pre:
                    self.suggestions.pop(tid, None)
                    self.patches.pop(tid, None)
                    self.take_w.pop(tid, None)
            chosen = None
            for sc, sid, what in sorted(tries):
                if sc + 0.03 < base and not self.check_fix(sid):
                    chosen = (sc, sid, what)
                    break
            for _, sid, _ in tries:
                if chosen and sid == chosen[1]:
                    continue
                if sid not in pre and sid in self.suggestions:
                    self.suggestions.pop(sid, None)
                    self.patches.pop(sid, None)
                    self.take_w.pop(sid, None)
            if chosen:
                self.suggestions[chosen[1]]["status"] = "accepted"
                self._render_cache.pop(("stem", k), None)
                self._render_cache.pop("mix", None)
                n_done += 1
                lines.append(f"The {k} in {sec['name']}: used {chosen[2]} (scored {score} before)")
        # then the weakest single bars: try rebuilding each from the bars that match it best
        bars = [x for x in (self.quality().get("weak_bars") or []) if x["score"] < 60][:10]
        for wb in bars:
            k = wb["stem"]
            a, b = int(wb["start"] * self.sr), min(int(wb["end"] * self.sr), self.stems[k].shape[1])
            if b - a < self.sr // 4 or k in NOT_REPAIRED:
                continue
            try:
                before_ids = set(self.suggestions)
                ids = self.rebuild_range(wb["start"], wb["end"], k)
            except Exception as e:
                log(f"improve: bar rebuild skipped: {e}")
                continue
            R = self.render_stem(k)[:, a:b]
            raw = self.stems[k][:, a:b]
            base, lv0 = self._seg_score(R, a, b)
            best = None
            for sid in ids or []:
                try:
                    sc, lv = self._seg_score(self._with_candidate(sid, a, b, R, raw), a, b)
                    sc += 0.5 * float(np.clip((lv0 - lv) / 10, 0, 1))
                except Exception:
                    continue
                if sc + 0.03 < base and not self.check_fix(sid) and (best is None or sc < best[0]):
                    best = (sc, sid)
            for sid in ids or []:
                if (best is None or sid != best[1]) and sid not in before_ids:
                    self.suggestions.pop(sid, None)
                    self.patches.pop(sid, None)
                    self.take_w.pop(sid, None)
            if best:
                self.suggestions[best[1]]["status"] = "accepted"
                self._render_cache.pop(("stem", k), None)
                self._render_cache.pop("mix", None)
                lines.append(f"The {k} at {int(wb['start'] // 60)}:{wb['start'] % 60:04.1f}: rebuilt (scored {wb['score']} before)")
        return lines, q

    # ------------------------------------------------------------------ for music software: tempo, markers, drum MIDI
    def _beats_per_bar(self):
        Ls = [L for _, L in self.windows if L]
        return int(np.median(Ls)) if Ls else 4

    def _markers(self):
        return [(float(sec["start"]), sec["name"]) for sec in self.sections]

    def export_tempo(self, folder, name):
        """The song's tempo and sections, for lining stems up in music software:
        a MIDI tempo map (a tempo change on every beat, section markers), Audacity labels, and a text summary."""
        from . import midi as MD
        os.makedirs(folder, exist_ok=True)
        files = []
        p = os.path.join(folder, f"{name} - tempo map.mid")
        MD.write(p, self.beat_times, self._beats_per_bar(), self._markers(), [], name=f"{name} tempo map")
        files.append(p)
        p = os.path.join(folder, f"{name} - section markers (Audacity labels).txt")
        with open(p, "w", encoding="utf-8") as f:
            for sec in self.sections:
                f.write(f"{sec['start']:.3f}\t{sec['end']:.3f}\t{sec['name']}\n")
        files.append(p)
        bt = np.asarray(self.beat_times)
        bpm = float(self.meta.get("bpm") or (60 / np.median(np.diff(bt)) if len(bt) > 2 else 0))
        drift = float(np.std(60 / np.diff(bt))) if len(bt) > 3 else 0.0
        p = os.path.join(folder, f"{name} - tempo.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"Tempo: {bpm:.2f} BPM (it varies by about {drift:.1f} BPM; the tempo map follows it beat by beat)\n")
            f.write(f"Time signature: {self._beats_per_bar()}/4\n")
            f.write(f"First beat at: {bt[0]:.3f} s\n" if len(bt) else "")
            f.write("All exported audio starts at 0 s; place the stems and the MIDI files at the start of the project.\n\n")
            f.write("Sections:\n")
            for sec in self.sections:
                f.write(f"  {int(sec['start'] // 60)}:{sec['start'] % 60:06.3f}  {sec['name']}\n")
        files.append(p)
        return files

    GM_DRUMS = {"kick": [36, 35], "snare / clap": [38, 40, 39], "hi-hat / cymbal": [42, 46, 49, 51], "tom / low drum": [45, 47, 50, 43]}
    GM_BY_STEM = {"kick": "kick", "snare": "snare / clap", "toms": "tom / low drum", "cymbals": "hi-hat / cymbal"}

    def export_drum_midi(self, path):
        """Every hit of every repeated drum sound as General MIDI drum notes, plus the hits the learned
        pattern restores under the singing, on the song's own tempo map. Loudness sets the velocity."""
        from . import midi as MD
        drums = [k for k in self.instruments if k in self.PERCUSSIVE]
        if not drums:
            return None
        restored = {}
        try:
            restored = self.find_pattern_hits()
        except Exception as e:
            log(f"drum MIDI: pattern hits skipped: {e}")
        notes, used = [], {}
        for k in drums:
            try:
                sd = self._sounds(k)
            except Exception as e:
                log(f"drum MIDI: {k} skipped: {e}")
                continue
            lv = sd["lv"]
            for snd in sd["sounds"]:
                kind = self.GM_BY_STEM.get(k) or snd["name"]
                choices = self.GM_DRUMS.get(kind, [37])
                n_used = used.get(kind, 0)
                note = choices[min(n_used, len(choices) - 1)]
                used[kind] = n_used + 1
                mem = snd.get("members") or []
                if not mem:
                    continue
                lo, hi = float(np.percentile(lv[mem], 5)), float(np.percentile(lv[mem], 95)) + 1e-6
                for i in mem:
                    vel = 45 + 82 * float(np.clip((lv[i] - lo) / (hi - lo), 0, 1))
                    notes.append((float(sd["pos"][i]) / self.sr, note, vel, 0.08))
                med = 45 + 82 * 0.5
                for q in restored.get((k, snd["id"]), []):
                    notes.append((q / self.sr, note, med, 0.08))
        if not notes:
            return None
        notes.sort()
        MD.write(path, self.beat_times, self._beats_per_bar(), self._markers(), [("Drums", 9, notes)],
                 name=f"{self.meta.get('name', 'song')} drums")
        return path

    # ------------------------------------------------------------------ samples: hits, notes and loops
    @staticmethod
    def _merge_clips(clips):
        """Blend equal-length clips into one: per-frequency median of their levels (so noise in any one
        drops out), with the timing and attack of the first (cleanest) one."""
        out = np.zeros_like(clips[0])
        for ch in range(clips[0].shape[0]):
            X = [stft(c[ch]) for c in clips]
            mag = np.median(np.stack([np.abs(x) for x in X]), axis=0) if len(X) > 2 else np.mean([np.abs(x) for x in X], axis=0)
            out[ch] = istft(mag * np.exp(1j * np.angle(X[0])), clips[0].shape[1])
        return out

    def _sample_ready(self, y):
        y = np.asarray(y, np.float32).copy()
        n = y.shape[1]
        fi, fo = min(n // 4, int(0.001 * self.sr)), min(n // 4, int(0.01 * self.sr))
        if fi:
            y[:, :fi] *= np.linspace(0, 1, fi, dtype=np.float32)
        if fo:
            y[:, -fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
        pk = float(np.abs(y).max())
        return y * (0.89 / pk) if pk > 1e-6 else y          # -1 dB peak

    def _truly_instrumental(self, a, b):
        """No singing at all in [a, b): the vocal track (and backing vocals, reverb tails) 45 dB under its usual
        level, so the original recording there is a clean instrumental."""
        v = np.asarray(self.stems["vocals"][:, a:b], np.float32)
        if "backing_vocals" in self.stems:
            v = v + np.asarray(self.stems["backing_vocals"][:, a:b], np.float32)
        if v.shape[1] < 64:
            return False
        ref = self.__dict__.get("_voc_loud")
        if ref is None:
            x = np.asarray(self.stems["vocals"], np.float32).mean(0)
            blk = x[:len(x) // 4096 * 4096].reshape(-1, 4096)
            ref = self.__dict__["_voc_loud"] = float(np.percentile((blk ** 2).mean(1), 95)) + 1e-12
        blk = v.mean(0)[:v.shape[1] // 2048 * 2048].reshape(-1, 2048)
        return bool(len(blk)) and float((blk ** 2).mean(1).max()) < ref * 10 ** (-45 / 10)

    SAMPLE_MIN_COUNT = 6       # a hit or note must repeat this often to count as a real part of the song
    SAMPLE_PER_STEM = 6        # at most this many hit/note samples per instrument (the most prominent ones)

    def _phrase_loops(self, bars=4):
        """For each kind of section (Verse, Chorus, ...): the cleanest `bars`-bar phrase of it, starting on a bar,
        where the least singing was and the music is at its usual level. [(kind, a, b, t)] in samples."""
        full = max((L for _, L in self.windows), default=0)
        by_kind = {}
        for si, sec in enumerate(self.sections):
            kind = sec.get("base") or "".join(c for c in sec["name"] if not c.isdigit()).strip() or "Part"
            by_kind.setdefault(kind, []).append(si)
        level = (self.inst_sum ** 2).mean(0)
        out = []
        for kind, sis in by_kind.items():
            best = None
            for si in sis:
                sec = self.sections[si]
                ws = [w for w in range(sec.get("start_bar", 0), sec.get("end_bar", 0)) if self.windows[w][1] == full]
                for j in range(len(ws) - bars + 1):
                    run = ws[j:j + bars]
                    if run[-1] - run[0] != bars - 1:
                        continue
                    s0 = self.windows[run[0]][0]
                    s1 = self.windows[run[-1]][0] + full
                    a, b = int(self.beats[s0] * HOP), int(self.beats[s1] * HOP)
                    voc = float(np.asarray(self.VA[s0:s1]).mean()) if len(self.VA) else 0.0
                    en = float(level[a:b].mean())
                    score = voc - 0.05 * np.log(en + 1e-12)          # least singing first, then fuller sound
                    if best is None or score < best[0]:
                        best = (score, a, b, self.beat_times[s0])
            if best:
                out.append((kind, best[1], best[2], best[3]))
        return out

    def export_samples(self, folder, raw=True, merged=True, fmt="wav"):
        """Write the song's reusable pieces, with context:
        - Loops: for every kind of section, its cleanest 4-bar and 2-bar phrase, starting on the bar, from the
          finished (repaired) instrumental and from each instrument, named with the section and tempo.
        - Hits and notes: only sounds that are a real part of the song (repeat often, are among the loudest on
          their track), cleanest copy and a merge of the cleanest copies.
        - Riffs: patterns that repeat at least 4 times. Returns the files written."""
        import re as _re
        safe = lambda t: _re.sub(r'[\\/:*?"<>|]+', "-", t).strip() or "sample"
        written = []

        def put(sub, name, y, shape=True):
            d = os.path.join(folder, safe(sub))
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, safe(name) + "." + fmt)
            y = self._sample_ready(y) if shape else np.asarray(y, np.float32)
            save_audio(p, y, self.sr, bits=24 if fmt == "wav" else 32)
            written.append(p)

        bt = np.diff(np.asarray(self.beat_times, float))
        bpm = int(round(60.0 / float(np.median(bt)))) if len(bt) else 0
        tempo = f" {bpm} BPM" if bpm else ""
        ts = lambda t: f"{int(t // 60)}m{t % 60:04.1f}s"

        def loop_ready(y):
            """Loops keep their level relation and get only tiny edge fades, so they cycle without clicks."""
            y = np.asarray(y, np.float32).copy()
            f = min(y.shape[1] // 8, int(0.004 * self.sr))
            if f:
                y[:, :f] *= np.linspace(0, 1, f, dtype=np.float32)
                y[:, -f:] *= np.linspace(1, 0, f, dtype=np.float32)
            return y

        # 1. musical loops of each section kind
        if raw:
            try:
                full = self.mix("repaired")
                pk = float(np.abs(full).max()) + 1e-9
                g = 0.89 / pk if pk > 0.89 else 1.0
                tracks = {k: self.render_stem(k) for k in self.instruments}
                vc_all = self._vocal_curve(0, self.original.shape[1])
                for bars in (4, 2):
                    for kind, a, b, t in self._phrase_loops(bars):
                        base = f"{kind} - {bars} bars{tempo} (from {ts(t)})"
                        # nobody sings here: the original recording is the cleanest version there is
                        src_ = self.original if self._truly_instrumental(a, b) else full
                        put("Loops - full instrumental", base, loop_ready(src_[:, a:b] * g), shape=False)
                        for k, y in tracks.items():
                            seg = y[:, a:b]
                            if float((seg ** 2).mean()) < 1e-7:
                                continue                          # this instrument doesn't play there
                            put(f"Loops - {k}", base, loop_ready(seg * g), shape=False)
            except Exception as e:
                log(f"samples: loops skipped: {e}")

        # 2. prominent hits and notes
        pre = int(0.005 * self.sr)
        for k in self.instruments:
            perc = k in self.PERCUSSIVE
            if not perc and not self._tonal(k):
                continue
            try:
                sd = self._sounds(k)
            except Exception as e:
                log(f"samples: hits skipped for {k}: {e}")
                continue
            x = self.stems[k]
            if not perc:
                continue          # pitched parts are exported as repeating samples of a half bar or more (below)
            cand = [snd for snd in sd["sounds"] if snd["count"] >= self.SAMPLE_MIN_COUNT]
            if not cand:
                continue
            lvl = {snd["id"]: float(np.median(sd["lv"][snd["members"]])) for snd in cand}
            top = max(lvl.values())
            cand = [snd for snd in cand if lvl[snd["id"]] >= top - np.log(10 ** 1.8)]    # within 18 dB of the loudest
            cand.sort(key=lambda snd: -(np.log(snd["count"]) + 0.5 * (lvl[snd["id"]] - top)))
            for rank, snd in enumerate(cand[:self.SAMPLE_PER_STEM]):
                ranked = snd.get("ranked") or [snd["best"]]
                p0 = int(sd["pos"][ranked[0]])
                L = int(min(sd["next"][ranked[0]] - p0, (1.5 if perc else 2.0) * self.sr))
                if p0 - pre < 0 or L < int(0.03 * self.sr):
                    continue
                best = x[:, p0 - pre:p0 + L]
                secn = self.sections[self._section_of(p0 / self.sr)]["name"] if self.sections else ""
                label = f"{rank + 1:02d} {snd['name']} - played {snd['count']}x" + (f" (cleanest in {secn})" if secn else "")
                if raw:
                    put(f"Hits - {k}", f"{label} - cleanest", best)
                if merged and len(ranked) >= 2:
                    clips = [best]
                    for j in ranked[1:self.BLEND_HITS]:
                        pj = int(sd["pos"][j])
                        if pj - pre - 64 < 0 or pj + L + 64 > x.shape[1]:
                            continue
                        sh = HT.align(best.mean(0), x[:, pj - pre:pj + L].mean(0), int(0.005 * self.sr))
                        q0 = pj - pre - sh
                        if q0 >= 0 and q0 + best.shape[1] <= x.shape[1]:
                            clips.append(x[:, q0:q0 + best.shape[1]])
                    if len(clips) >= 2:
                        put(f"Hits - {k}", f"{label} - merged from {len(clips)}", self._merge_clips(clips))

        # 2b. pitched instruments: whole notes (to where they really end) and the short phrases they repeat
        try:
            vc_all = self._vocal_curve(0, self.original.shape[1])
            for k in [x for x in self.instruments if self._tonal(x)]:
                try:
                    dm = self.drum_map(k)
                except Exception:
                    continue
                if not dm["rows"] or dm["rows"][0].get("midi") is None:
                    continue
                x = self.stems[k]
                pos = dm["_pos"]
                env = np.sqrt(np.convolve(x.mean(0) ** 2, np.ones(1024) / 1024, mode="same"))
                def note_end(i):
                    p0 = int(pos[i])
                    nx = int(pos[i + 1]) if i + 1 < len(pos) else x.shape[1]
                    pk = float(env[p0:min(nx, p0 + int(0.15 * self.sr))].max(initial=0))
                    lim = min(nx, p0 + int(4 * self.sr))
                    quiet = np.flatnonzero(env[p0 + int(0.05 * self.sr):lim] < pk * 10 ** (-30 / 20))
                    return p0 + int(0.05 * self.sr) + int(quiet[0]) if len(quiet) else lim
                clean_i = lambda i: float(vc_all[int(pos[i]):note_end(i)].max(initial=0)) < 0.05
                for r in dm["rows"]:                       # whole notes
                    idx = [int(i) for i in dm["_clusters"][r["id"]] if clean_i(int(i))]
                    if len(idx) < 2:
                        continue
                    ends = {i: note_end(i) for i in idx}
                    best = max(idx, key=lambda i: ends[i] - int(pos[i]))       # the fullest-sounding copy
                    a0, b0 = max(0, int(pos[best]) - pre), ends[best]
                    label = f"{r['name']} - played {r['count']}x"
                    if raw:
                        put(f"Notes - {k}", f"{label} - cleanest", x[:, a0:b0])
                    if merged:
                        L = b0 - a0
                        clips = [x[:, a0:b0]] + [x[:, max(0, int(pos[i]) - pre):max(0, int(pos[i]) - pre) + L]
                                                 for i in idx if i != best and int(pos[i]) - pre + L <= x.shape[1]][:4]
                        if len(clips) >= 2:
                            put(f"Notes - {k}", f"{label} - merged from {len(clips)}", self._merge_clips(clips))
                # phrases: the same few notes in the same rhythm, repeated (shorter than a bar is fine)
                bt = np.asarray(self.beat_times, float)
                seq = []
                note_of = {}
                for r in dm["rows"]:
                    for i in dm["_clusters"][r["id"]]:
                        note_of[int(i)] = r["midi"]
                for i in sorted(note_of):
                    j = int(np.searchsorted(bt, pos[i] / self.sr, side="right")) - 1
                    if 0 <= j < len(bt) - 1:
                        seq.append((i, note_of[i], j + (pos[i] / self.sr - bt[j]) / (bt[j + 1] - bt[j])))
                found = {}
                for L in (6, 5, 4):
                    for a_ in range(len(seq) - L):
                        win = seq[a_:a_ + L]
                        if win[-1][2] - win[0][2] > 8:          # at most 2 bars long
                            continue
                        key = tuple((w[1] - win[0][1], round((w[2] - win[0][2]) * 4)) for w in win)
                        found.setdefault((L, key), []).append(a_)
                used, n_ph = set(), 0
                for (L, key), starts_ in sorted(found.items(), key=lambda kv: -(len(kv[1]) * kv[0][0])):
                    starts_ = [a_ for a_ in starts_ if not any(a_ + d in used for d in range(L))]
                    if len(starts_) < 3 or n_ph >= 4:
                        continue
                    clean_s = [a_ for a_ in starts_ if clean_i(seq[a_][0]) and clean_i(seq[a_ + L - 1][0])]
                    if not clean_s:
                        continue
                    for a_ in starts_:
                        used.update(range(a_, a_ + L))
                    a_ = clean_s[0]
                    p0, p1 = max(0, int(pos[seq[a_][0]]) - pre), note_end(seq[a_ + L - 1][0])
                    n_ph += 1
                    t0 = p0 / self.sr
                    secn = self.sections[self._section_of(t0)]["name"] if self.sections else ""
                    label = f"phrase {n_ph} - {L} notes - repeats {len(starts_)}x{tempo} ({secn}, {ts(t0)})"
                    if raw:
                        put(f"Phrases - {k}", f"{label} - cleanest", x[:, p0:p1])
                    if merged and len(clean_s) >= 2:
                        clips = [x[:, p0:p1]]
                        for a2 in clean_s[1:5]:
                            q0, q1 = max(0, int(pos[seq[a2][0]]) - pre), note_end(seq[a2 + L - 1][0])
                            c = x[:, q0:q1]
                            if c.shape[1] < 64:
                                continue
                            xi = np.linspace(0, c.shape[1] - 1, p1 - p0)
                            clips.append(np.stack([np.interp(xi, np.arange(c.shape[1]), c[ch]) for ch in range(c.shape[0])]).astype(np.float32))
                        if len(clips) >= 2:
                            put(f"Phrases - {k}", f"{label} - merged from {len(clips)}", self._merge_clips(clips))
        except Exception as e:
            log(f"samples: notes and phrases skipped: {e}")

        # 3. repeating samples at their true length (1 to 4 bars), from the whole instrumental and
        #    from each instrument: the cleanest copy and a merge of the cleanest copies
        try:
            srcs = [("full instrumental", self.mix("repaired"))] + [(k, self.render_stem(k)) for k in self.instruments]
            vc_all = self._vocal_curve(0, self.original.shape[1])
            sung = lambda b0, b1: float(vc_all[int(self.beats[b0] * HOP):int(self.beats[b1] * HOP)].max(initial=0)) > 0.05
            for src, y in srcs:
                groups = []
                for g in self.find_loops(y, drums=src in self.PERCUSSIVE):
                    clean = [m for m in g["members"] if not sung(*m)]
                    if not clean:
                        continue                      # every copy has singing over it: not a clean sample
                    groups.append({**g, "members": clean, "beats": clean[0]})
                if src == "full instrumental" and groups and all(self._truly_instrumental(*[int(self.beats[q] * HOP) for q in g["beats"]]) for g in groups):
                    y = self.original               # the copies chosen have no singing at all: use the real recording
                for li, g in enumerate(groups):
                    t = self.beat_times[g["beats"][0]]
                    secn = self.sections[self._section_of(t)]["name"] if self.sections else ""
                    ln = {0.5: "half bar", 1: "1 bar"}.get(g["len_bars"], f"{g['len_bars']} bars")
                    label = f"{chr(65 + li)} - {ln} - repeats {g['count']}x{tempo} ({secn}, {ts(t)})"
                    if raw:
                        put(f"Repeating samples - {src}", f"{label} - cleanest", loop_ready(self._loop_audio(y, g, False)), shape=False)
                    if merged and len(g["members"]) >= 2:
                        put(f"Repeating samples - {src}", f"{label} - merged from {min(5, len(g['members']))}",
                            loop_ready(self._loop_audio(y, g, True)), shape=False)
        except Exception as e:
            log(f"samples: repeating samples skipped: {e}")
        return written

    # ------------------------------------------------------------------ smart cleanup: learn what belongs
    CLEAN_FLOOR_DB = -12      # turn down what doesn't belong by at most this much (never a hard mute)

    def _band_gain_apply(self, X, gb):
        """Per-band gains (bands, frames) -> per-bin gains on a complex STFT."""
        B = self.B
        w = B.sum(0) + 1e-9
        return X * ((B.T @ gb) / w[:, None]).astype(np.float32)

    def _noise_profile(self, k):
        """The steady noise of a track (hiss, vinyl surface, hum): per frequency, its level in the quietest
        stretches of the song."""
        cache = self.__dict__.setdefault("_noise_prof", {})
        if k in cache:
            return cache[k]
        x = self.stems[k].mean(0)
        L = self.sr
        lev = [float(np.mean(x[i:i + L] ** 2)) for i in range(0, max(1, len(x) - L), L)]
        quiet = np.argsort(lev)[:max(3, len(lev) // 10)]
        mags = [np.abs(stft(x[i * L:(i + 1) * L])) for i in quiet if (i + 1) * L <= len(x)]
        prof = np.percentile(np.concatenate(mags, axis=1), 50, axis=1) if mags else None
        cache[k] = prof
        return prof

    def smart_clean(self, sung_only=True):
        """Learn what belongs on each track and turn down the rest, section by section:
          - drums: each sound's normal attack and decay, learned from its clean hits; anything louder than that
            between and after hits (clicks, crackle, smeared consonants, separation chirps) comes down,
          - pitched parts: during each note, what isn't at that note's pitch and overtones (bleed singing other
            notes, artifacts) comes down,
          - repeating loops: what a sung copy has that the clean copies don't comes down,
          - every track: its steady noise (hiss, vinyl surface), learned from the quietest moments.
        Gentle (at most CLEAN_FLOOR_DB), only under the singing unless sung_only=False (noise: everywhere).
        One review item per track and section. Returns ids."""
        ids = []
        sr, n_all = self.sr, self.original.shape[1]
        floor = 10 ** (self.CLEAN_FLOOR_DB / 20)
        vc = self._vocal_curve(0, n_all)
        from scipy.ndimage import uniform_filter1d, maximum_filter
        for k in [x for x in self.instruments if x not in NOT_REPAIRED]:
            raw = np.asarray(self.stems[k], np.float32)
            n = raw.shape[1]
            prof = self._noise_profile(k)
            perc = k in self.PERCUSSIVE
            try:
                dm = self.drum_map(k)
            except Exception:
                dm = None
            # learned shapes: each sound's band envelope over its first ~0.4 s, from its clean hits
            tmpl = {}
            if dm and dm["rows"] and (perc or dm["rows"][0].get("midi") is None):
                pos = dm["_pos"]
                T = int(0.4 * sr / HOP)
                for r in dm["rows"]:
                    c = [int(i) for i in dm["_clusters"][r["id"]] if vc[min(int(pos[i]) + int(0.02 * sr), n_all - 1)] < 0.05][:8]
                    envs = []
                    for i in c:
                        a = int(pos[i])
                        E = VF.band_energy(raw[:, a:a + (T + 2) * HOP].mean(0), self.B)[:, :T]
                        if E.shape[1] == T and E.max() > 0:
                            envs.append(E / E[:, :3].max())
                    if envs:
                        tmpl[r["id"]] = np.median(envs, axis=0)
            # the note mask only on the bass (one note at a time); on mixed tracks it would remove the other instruments
            notes = dm if k == "bass" and dm and dm["rows"] and dm["rows"][0].get("midi") is not None else None
            loops = []
            try:
                loops = self.find_loops(raw, drums=perc, limit=6)
            except Exception:
                pass
            for si, sec in enumerate(self.sections):
                a, b = int(sec["start"] * sr), min(n, int(sec["end"] * sr))
                if b - a < sr // 2:
                    continue
                sung = float(vc[a:b].mean()) > 0.05
                lo, hi = max(0, a - XFADE), min(n, b + XFADE)
                seg = raw[:, lo:hi]
                stages = []                                 # (kind, cleaned seg)
                cur = seg.copy()
                f0 = lo // HOP
                nfr = (hi - lo) // HOP + 1
                wv = np.clip(uniform_filter1d(vc[lo:hi], int(0.1 * sr)) * 4, 0, 1)[::HOP][:nfr] if sung_only else np.ones(nfr)
                wv = np.pad(wv, (0, max(0, nfr - len(wv))), constant_values=wv[-1] if len(wv) else 1)
                def run(kind, gain_fn):
                    nonlocal cur
                    out = np.empty_like(cur)
                    for c in range(cur.shape[0]):
                        X = stft(cur[c])
                        G = gain_fn(X)
                        out[c] = istft(X * G, cur.shape[1])
                    stages.append((kind, out - cur))
                    cur = out
                # 1. steady noise, everywhere (spectral subtraction, gentle)
                if prof is not None:
                    def g_noise(X):
                        m = np.abs(X)
                        return np.clip((m - 1.5 * prof[:, None]) / (m + 1e-9), 0.3, 1).astype(np.float32)
                    run("noise", g_noise)
                if not sung and sung_only:
                    pass
                elif tmpl:
                    # 2. drums: what each sound's learned attack and decay allow, at each hit
                    hits = [(h[0], h[1]) for h in dm["hits"] if lo / sr - 0.4 <= h[0] < hi / sr]
                    def g_drum(X):
                        Eb = self.B @ (np.abs(X) ** 2)
                        allowed = np.full_like(Eb, 0.0)
                        for t, sid in hits:
                            tp = tmpl.get(sid)
                            if tp is None:
                                continue
                            f = int(round((t * sr - lo) / HOP))
                            if f >= Eb.shape[1] or f + tp.shape[1] <= 0:
                                continue
                            if f >= 0:
                                lvl = Eb[:, f:f + 3].max(axis=1, keepdims=True)
                            else:                          # a hit just before this part: its decay carries in
                                lvl = Eb[:, :3].max(axis=1, keepdims=True) / (tp[:, -f:-f + 1] + 1e-6)
                            e0, e1 = max(0, f), min(Eb.shape[1], f + tp.shape[1])
                            if e1 > e0:
                                allowed[:, e0:e1] = np.maximum(allowed[:, e0:e1], tp[:, e0 - f:e1 - f] * lvl * 2.0)  # +3 dB room
                        allowed = maximum_filter(allowed, size=(1, 3))
                        gb = np.clip(np.sqrt(allowed / (Eb + 1e-12)), floor, 1)
                        gb = 1 - (1 - gb) * wv[None, :gb.shape[1]]
                        return ((self.B.T @ gb) / (self.B.sum(0)[:, None] + 1e-9)).astype(np.float32)
                    run("pattern", g_drum)
                elif notes is not None:
                    # 3. pitched: keep the note's pitch and overtones while it plays
                    pos = notes["_pos"]
                    midi_of = {int(i): r["midi"] for r in notes["rows"] for i in notes["_clusters"][r["id"]]}
                    fr = np.fft.rfftfreq(N_FFT, 1 / sr)
                    spans = []
                    for i, m in midi_of.items():
                        p0 = int(pos[i])
                        if lo <= p0 < hi:
                            nx = int(pos[i + 1]) if i + 1 < len(pos) else hi
                            spans.append((p0 + int(0.03 * sr), min(nx, p0 + int(4 * sr)), 440 * 2 ** ((m - 69) / 12)))
                    def g_note(X):
                        G = np.ones(X.shape, np.float32)
                        for s0, s1, f0_ in spans:
                            c0, c1 = max(0, (s0 - lo) // HOP), min(X.shape[1], (s1 - lo) // HOP)
                            if c1 <= c0:
                                continue
                            h = np.arange(1, int(fr[-1] / f0_) + 1)[:40] * f0_
                            keep = np.zeros(len(fr), bool)
                            for hf in h:
                                keep |= np.abs(fr - hf) <= max(0.03 * hf, 12)
                            keep |= fr > h[-1] if len(h) else True
                            g = np.where(keep, 1.0, floor).astype(np.float32)
                            g = uniform_filter1d(g, 3)
                            G[:, c0:c1] = np.minimum(G[:, c0:c1], (1 - (1 - g)[:, None] * wv[None, c0:c1]))
                        return G
                    run("notes", g_note)
                # 4. loops: what a sung copy has that the clean copies don't
                if (sung or not sung_only) and loops:
                    covered = []
                    for g in loops:
                        span_ = lambda u: (int(self.beats[u[0]] * HOP), int(self.beats[u[1]] * HOP))
                        clean_u = [u for u in g["members"] if float(vc[slice(*span_(u))].max(initial=0)) < 0.05]
                        if len(clean_u) < 2:
                            continue
                        refa = self._loop_audio(raw, {**g, "members": clean_u, "beats": clean_u[0]}, True)
                        for u in g["members"]:
                            ua, ub = span_(u)
                            if ua >= lo and ub <= hi and float(vc[ua:ub].mean()) > 0.2:
                                covered.append((ua, ub, refa))
                    if covered:
                        def g_loop(X):
                            G = np.ones(X.shape, np.float32)
                            for ua, ub, refa in covered:
                                c0, c1 = (ua - lo) // HOP, min(X.shape[1], (ub - lo) // HOP)
                                if c1 - c0 < 4:
                                    continue
                                R = np.abs(stft(refa.mean(0)))
                                xi = np.linspace(0, R.shape[1] - 1, c1 - c0).astype(int)
                                Rm = maximum_filter(R[:, xi], size=(3, 3)) * 1.6
                                G[:, c0:c1] = np.clip(Rm / (np.abs(X[:, c0:c1]) + 1e-9), floor, 1)
                            return G
                        run("loops", g_loop)
                for kind, d in stages:
                    tot = float((seg ** 2).sum()) + 1e-12
                    rem = float((d ** 2).sum())
                    if rem < 1e-4 * tot:
                        continue                                # hardly changes anything here
                    rid = f"sc_{kind}_{k}_{si}"
                    if rid in self.suggestions and self.suggestions[rid]["status"] != "pending" and not self.suggestions[rid].get("by_build"):
                        continue
                    what = {"noise": "steady noise (hiss, vinyl surface)", "pattern": "what isn't part of each drum sound's hit and decay",
                            "notes": "what isn't at the notes being played", "loops": "what the clean copies of this loop don't have"}[kind]
                    self.suggestions[rid] = {
                        "id": rid, "type": "bleed", "stem": k, "status": "pending", "start": lo / sr, "end": hi / sr,
                        "section": si, "conf": 0.8, "severity": round(min(1.0, 10 * rem / tot), 3), "strength": 1.0,
                        "donors": [], "smart": kind, "removed_db": round(float(10 * np.log10(rem / tot + 1e-12)), 1),
                        "note": f"Turns down {what} on the {k} in {sec['name']} (learned from the rest of the song)"}
                    self.patches[rid] = (lo, d.astype(np.float16))
                    ids.append(rid)
        return ids

    # ------------------------------------------------------------------ rebuild from notes / from patterns
    def _note_end(self, x_env, pos, i, n):
        p0 = int(pos[i])
        nx = int(pos[i + 1]) if i + 1 < len(pos) else n
        pk = float(x_env[p0:min(nx, p0 + int(0.15 * self.sr))].max(initial=0))
        lim = min(nx, p0 + int(4 * self.sr))
        quiet = np.flatnonzero(x_env[p0 + int(0.05 * self.sr):lim] < pk * 10 ** (-30 / 20))
        return p0 + int(0.05 * self.sr) + int(quiet[0]) if len(quiet) else lim

    def make_note_repairs(self, ks=None):
        """Bass (and other pitched parts): notes the singing damaged are rebuilt AS THOSE NOTES, from a merge of
        the clean copies of the same note elsewhere in the song (or the nearest clean note, retuned up to 3
        semitones, when that note is never heard clean). A note counts as damaged when it's under the singing and
        clearly weaker than its clean copies. One review item per section. Returns ids."""
        ids = []
        sr = self.sr
        # only tracks that play one note at a time (the bass): on a track holding several instruments ("other",
        # guitar, piano) one detected note doesn't describe what's playing, and rebuilding it loses the rest
        for k in (ks or [x for x in self.instruments if x == "bass"]):
            try:
                dm = self.drum_map(k)
            except Exception as e:
                log(f"note repairs skipped for {k}: {e}")
                continue
            if not dm["rows"] or dm["rows"][0].get("midi") is None:
                continue
            x = self.stems[k]
            n = x.shape[1]
            pos = dm["_pos"]
            env = np.sqrt(np.convolve(x.mean(0) ** 2, np.ones(1024) / 1024, mode="same"))
            vc = self._vocal_curve(0, n)
            pre = int(0.003 * sr)
            midi_of = {}
            for r in dm["rows"]:
                for i in dm["_clusters"][r["id"]]:
                    midi_of[int(i)] = r["midi"]
            ends = {i: self._note_end(env, pos, i, n) for i in midi_of}
            sung = lambda i: float(vc[int(pos[i]):ends[i]].mean()) > 0.3
            lvl = lambda i: float(np.sqrt(np.mean(x[:, int(pos[i]):ends[i]] ** 2) + 1e-12))
            clean = {}
            for i, m in midi_of.items():
                if float(vc[int(pos[i]):ends[i]].max(initial=0)) < 0.05 and ends[i] - int(pos[i]) > int(0.08 * sr):
                    clean.setdefault(m, []).append(i)
            med = {m: float(np.median([lvl(i) for i in v])) for m, v in clean.items()}
            by_sec = {}
            for i, m in midi_of.items():
                if not sung(i):
                    continue
                src_m = m if m in clean else min(clean, key=lambda q: abs(q - m), default=None)
                if src_m is None or abs(src_m - m) > 3:
                    continue
                if lvl(i) > med[src_m] * 10 ** (-3 / 20):
                    continue                                  # about as strong as its clean copies: leave it
                a0, b0 = max(0, int(pos[i]) - pre), ends[i]
                L = b0 - a0
                ratio = 2 ** ((m - src_m) / 12)
                clips = []
                for j in clean[src_m][:5]:
                    q0 = max(0, int(pos[j]) - pre)
                    need = int(L * ratio) + 2
                    c = np.asarray(x[:, q0:q0 + need], np.float32)
                    if c.shape[1] < need * 0.8:
                        continue
                    if ratio != 1:                            # retune by resampling (a few semitones at most)
                        xi = np.arange(L) * ratio
                        c = np.stack([np.interp(xi, np.arange(c.shape[1]), c[ch], right=0) for ch in range(c.shape[0])]).astype(np.float32)
                    clips.append(np.pad(c[:, :L], ((0, 0), (0, max(0, L - c.shape[1])))))
                if not clips:
                    continue
                y = self._merge_clips(clips) if len(clips) >= 2 else clips[0]
                y *= med[src_m] / (float(np.sqrt(np.mean(y ** 2) + 1e-12)))
                by_sec.setdefault(self._section_of(a0 / sr), []).append((a0, y, m, src_m))
            for si, items in by_sec.items():
                rid = f"nt_{k}_{si}"
                if rid in self.suggestions and self.suggestions[rid]["status"] != "pending" and not self.suggestions[rid].get("by_build"):
                    continue                                  # you decided this one: leave it
                lo = max(0, min(a for a, *_ in items) - XFADE)
                hi = min(n, max(a + y.shape[1] for a, y, *_ in items) + XFADE)
                tgt = np.asarray(x[:, lo:hi], np.float32).copy()
                W = np.zeros(hi - lo, np.float32)
                rmp = int(0.008 * sr)
                for a, y, m, src_m in items:
                    L = min(y.shape[1], hi - a)
                    w = np.ones(L, np.float32)
                    w[:rmp] = np.linspace(0, 1, rmp)
                    w[-rmp:] = np.linspace(1, 0, rmp)
                    tgt[:, a - lo:a - lo + L] = y[:, :L]
                    W[a - lo:a - lo + L] = np.maximum(W[a - lo:a - lo + L], w)
                sec = self.sections[si]
                names = sorted({self.NOTE_NAMES[m % 12] + str(m // 12 - 1) for _, _, m, _ in items})
                retuned = sum(1 for *_, m, sm in items if m != sm)
                self.suggestions[rid] = {
                    "id": rid, "type": "alt", "stem": k, "status": "pending", "start": lo / sr, "end": hi / sr,
                    "section": si, "conf": 0.9, "severity": 0.0, "strength": 1.0, "donors": [], "alt_mode": "notes",
                    "blend": 0.0,
                    "note": f"Rebuilds {len(items)} damaged {k} note{'s' if len(items) != 1 else ''} in {sec['name']} "
                            f"({', '.join(names[:6])}{'...' if len(names) > 6 else ''}) from clean copies of the same note"
                            + (f"; {retuned} retuned from a nearby note" if retuned else "")}
                self.patches[rid] = (lo, (W[None, :] * (tgt - x[:, lo:hi])).astype(np.float16))
                self.take_w[rid] = (lo, W.astype(np.float16))
                ids.append(rid)
        self.check_ids(ids)
        return ids

    def make_pattern_rebuilds(self, ks=None):
        """For loop-based songs: in parts with singing, each instrument's loop is played again from a merge of
        its clean copies (stretched to each copy's exact length), instead of patching the damaged copy. Only
        loops (1, 2 or 4 bars) with at least two copies heard clean. One review item per section. Returns ids."""
        ids = []
        sr = self.sr
        vc = self._vocal_curve(0, self.original.shape[1])
        for k in (ks or [x for x in self.instruments if x not in NOT_REPAIRED]):
            raw = self.stems[k]
            n = raw.shape[1]
            try:
                groups = self.find_loops(raw, drums=k in self.PERCUSSIVE, limit=8)
            except Exception as e:
                log(f"pattern rebuild skipped for {k}: {e}")
                continue
            by_sec = {}
            for g in groups:
                span = lambda u: (int(self.beats[u[0]] * HOP), int(self.beats[u[1]] * HOP))
                clean = [u for u in g["members"] if float(vc[slice(*span(u))].max(initial=0)) < 0.05]
                dirty = [u for u in g["members"] if float(vc[slice(*span(u))].mean()) > 0.2]
                if len(clean) < 2 or not dirty:
                    continue
                ref = self._loop_audio(raw, {**g, "members": clean, "beats": clean[0]}, True)
                for u in dirty:
                    a, b = span(u)
                    if b - a < 64 or b > n:
                        continue
                    xi = np.linspace(0, ref.shape[1] - 1, b - a)
                    y = np.stack([np.interp(xi, np.arange(ref.shape[1]), ref[c]) for c in range(ref.shape[0])]).astype(np.float32)
                    by_sec.setdefault(self._section_of(a / sr), []).append((a, b, y, g["len_bars"]))
            for si, items in by_sec.items():
                rid = f"pt_{k}_{si}"
                if rid in self.suggestions and self.suggestions[rid]["status"] != "pending" and not self.suggestions[rid].get("by_build"):
                    continue
                lo = max(0, min(a for a, *_ in items) - XFADE)
                hi = min(n, max(b for _, b, *_ in items) + XFADE)
                tgt = np.asarray(raw[:, lo:hi], np.float32).copy()
                W = np.zeros(hi - lo, np.float32)
                rmp = int(0.01 * sr)
                for a, b, y, _ in sorted(items, key=lambda t: t[0]):
                    L = b - a
                    w = np.ones(L, np.float32)
                    w[:rmp] = np.linspace(0, 1, rmp)
                    w[-rmp:] = np.linspace(1, 0, rmp)
                    tgt[:, a - lo:b - lo] = tgt[:, a - lo:b - lo] * (1 - w) + y * w
                    W[a - lo:b - lo] = np.maximum(W[a - lo:b - lo], w)
                sec = self.sections[si]
                self.suggestions[rid] = {
                    "id": rid, "type": "alt", "stem": k, "status": "pending", "start": lo / sr, "end": hi / sr,
                    "section": si, "conf": 0.85, "severity": 0.0, "strength": 1.0, "donors": [], "alt_mode": "pattern",
                    "blend": 0.0,
                    "note": f"Plays the {k} loop again in {sec['name']} ({len(items)} cop{'ies' if len(items) != 1 else 'y'}) "
                            f"from a merge of its clean copies, instead of patching the damaged ones"}
                self.patches[rid] = (lo, (W[None, :] * (tgt - raw[:, lo:hi])).astype(np.float16))
                self.take_w[rid] = (lo, W.astype(np.float16))
                ids.append(rid)
        self.check_ids(ids)
        return ids

    def start_over(self):
        """Back to the song as it was split and analysed: every fix undecided again, levels, kept parts, mutes,
        cuts, clean tracks and grid edits cleared (the split itself and the analysis stay). Undo brings it back."""
        with self.lock:
            self._push("Start over", list(self.suggestions))
            for x in self.suggestions.values():
                for key in ("by_build", "by_build_was", "skip_reason"):
                    x.pop(key, None)
                x["status"] = "pending"
            self.mixer = {"stem_gain": {}, "mute": {}, "solo": {}, "section_gain": {}, "keep": [], "vocal_lift": {},
                          "mute_ranges": [], "sides": [], "mud_cut": {}, "tone_eq": {}, "seams": [], "eq_ranges": []}
            for k in list((self.meta.get("refs") or {})):
                self.remove_reference(k)
            self.meta.pop("quality", None)
            self._render_cache.clear()
            for c in ("_drum_map", "_drum_samples", "_metric_cache"):
                self.__dict__.pop(c, None)
            for k in self.instruments:
                self._refresh_after(k, 0, self.stems[k].shape[1])
            self._compute_grid()

    # ------------------------------------------------------------------ build the best instrumental
    AUTO_STEPS = ("keep", "gaps", "refs", "bestof", "versions", "bars", "hits", "pattern", "notes", "loops", "repairs",
                  "recover", "deep", "leftovers", "smartclean",
                  "clicks", "improve", "mutes", "acappella", "acappella_hard", "notch", "tone", "seams")

    def vocal_gaps(self, min_len=1.0):
        """Gaps between the vocal lines (a second or more) where the singer is silent: the original recording
        is a clean instrumental there. Strict: the vocal track must be 45 dB under its usual level, with margins
        for breaths before a line and reverb after it (and backing vocals count as singing). [(start, end)]"""
        if self.meta.get("separation") == "Imported stems" or not self.instruments:
            return []
        hop = 1024
        def frames(y):
            x = np.asarray(y, np.float32).mean(0)
            n = len(x) // hop
            return (x[:n * hop].reshape(n, hop) ** 2).mean(1)
        v = frames(self.stems["vocals"])
        if "backing_vocals" in self.stems:
            v = v + frames(self.stems["backing_vocals"])[:len(v)]
        loud = float(np.percentile(v, 95)) + 1e-12
        sing = v > loud * 10 ** (-45 / 10)
        from scipy.ndimage import binary_dilation
        fr = self.sr / hop
        pre, post = int(0.15 * fr), int(0.5 * fr)
        sing = binary_dilation(sing, np.ones(pre + post + 1, bool), origin=(post - pre) // 2)
        out, j = [], 0
        while j < len(sing):
            if not sing[j]:
                e = j
                while e + 1 < len(sing) and not sing[e + 1]:
                    e += 1
                a, b = j / fr, (e + 1) / fr
                if b - a >= min_len:
                    out.append((round(a, 3), round(b, 3)))
                j = e + 1
            else:
                j += 1
        return out

    def _free_sections(self):
        """Sections with no singing: the original recording already is a clean instrumental there."""
        if self.meta.get("separation") == "Imported stems" or not len(self.VA):
            return []
        out = []
        for sec in self.sections:
            a = int(np.searchsorted(self.beat_times, sec["start"]))
            b = int(np.searchsorted(self.beat_times, sec["end"]))
            va = np.asarray(self.VA[a:max(b, a + 1)])
            if len(va) and float(va.max()) < 0.04:
                out.append(sec)
        return out

    # A simple build is the default: the best split, the original wherever nobody sings, your clean tracks,
    # careful repairs and cleanup. These guess more (and can guess wrong), so they're extras you tick yourself.
    EXTRA_STEPS = ("versions", "bars", "pattern", "loops", "recover", "deep", "improve", "mutes", "notch", "tone",
                   "acappella_hard")

    def _safe_runs(self, aggressive):
        try:
            return self.vocal_only_runs(aggressive)
        except Exception as e:
            log(f"a cappella check skipped: {e}")
            return []

    def build_plan(self):
        """What Build the best instrumental would do, without changing anything."""
        if self.stage != "detected":
            raise RuntimeError("Find damage first (step 3), then build.")
        sugs = list(self.suggestions.values())
        pend = lambda t: [x for x in sugs if x["type"] in t and x["status"] == "pending"]
        free = self._free_sections()
        try:
            gaps = [g for g in self.vocal_gaps()
                    if not any(sc["start"] <= g[0] and sc["end"] >= g[1] for sc in free)]
        except Exception as e:
            log(f"vocal gaps skipped: {e}")
            gaps = []
        hits = [h for h in self.best_hits() if not h["status"]]
        steps = [
            ("keep", "Use the original recording where nobody sings",
             f"{len(free)} part{'s' if len(free) != 1 else ''} ({', '.join(s['name'] for s in free[:4])}{'...' if len(free) > 4 else ''}): "
             "the cleanest sound there is. Fixes inside them are skipped.", len(free)),
            ("gaps", "...and between the vocal lines",
             f"{len(gaps)} gap{'s' if len(gaps) != 1 else ''} of a second or more where the singer is silent "
             f"({sum(b - a for a, b in gaps):.0f} s in all): the original recording takes over there, with crossfades "
             "and margins for breaths and reverb.", len(gaps)),
            ("refs", "Use your clean tracks",
             "Where the singing damaged an instrument you have a clean track of, that track is used (lined up with the "
             "song); the other fixes skip those parts.",
             len([x for x in sugs if x.get("alt_mode") == "imported" and x["status"] == "pending"])),
            ("bestof", "Build each instrument from the cleanest model, bar by bar",
             "Uses the other models' splits you already have." if self.alt_modes() else
             "Needs another model's split first (Try another model here); skipped for now.", len(self.alt_modes())),
            ("versions", "Use the cleanest take of parts that repeat",
             "Whole parts replaced by a cleaner copy of the same part, when it's a close match.",
             len([x for x in self.best_versions() if not x["status"]])),
            ("bars", "Rebuild riffs and patterns that repeat", "Damaged repeats replaced by the cleanest one, where no best version covers them.",
             len([g for g in self.best_bars() if not g["status"]])),
            ("hits", "Rebuild drum hits and note starts from a blend of their cleanest copies",
             "Drums: used instead of ordinary repairs on the same hits, so nothing is filled in twice.",
             len(hits)),
            ("pattern", "Learn the drum and bass patterns and restore hits and notes the vocals took",
             "Only where the pattern is near-certain one belongs, the original recording has that sound there, and the "
             "track is missing it.", len([x for x in self.instruments if x in self.PERCUSSIVE or x == "bass"])),
            ("notes", "Rebuild damaged bass notes as the notes they are",
             "Notes under the singing that came out clearly weaker are made again from clean copies of the same note "
             "elsewhere in the song (or a nearby note retuned a little).",
             1 if "bass" in self.instruments else 0),
            ("loops", "Loop-based songs: play each loop again from its clean copies",
             "Where an instrument repeats a 1-4 bar loop, copies under the singing are replaced by a merge of the clean "
             "copies. Best for electronic and loop-based music; off by default.",
             len([x for x in self.instruments if x not in NOT_REPAIRED])),
            ("smartclean", "Smart cleanup: learn what belongs on each track and turn down the rest",
             "Drums: anything beyond each sound's normal hit and decay. Pitched parts: what isn't at the notes being "
             "played. Loops: what the clean copies don't have. Every track: steady hiss and vinyl noise. Gentle "
             "(at most 12 dB), mainly under the singing.", len([x for x in self.instruments if x not in NOT_REPAIRED])),
            ("repairs", "Apply the repairs with a close match",
             "Only where nothing above already covers that instrument, and never ones that don't fit the original.",
             len([x for x in pend(("repair",)) if x.get("recommended")])),
            ("recover", "Bring back details the split put in the vocal track",
             "Synths, effects and small parts often get filed as \"vocals\". One quick model run on the vocal track finds "
             "them and brings back the clear ones. Off by default: on some songs it brings back noise too.",
             1 if self._recover_target() else 0),
            ("deep", "Second vocal-removal pass on the instruments",
             "Runs the vocal model again on each instrument (not the drums). Off by default: slow, and the notches below "
             "usually do enough.", len([k for k in self.instruments if k not in NOT_REPAIRED and k not in self.PERCUSSIVE])),
            ("leftovers", "Remove leftover singing and \"s\" sounds", "From the instruments.", len(pend(("bleed", "ess")))),
            ("clicks", "Fix clicks and dropouts", "A few milliseconds each.", len(pend(("glitch",)))),
            ("improve", "Improve weak sections", "Every section and instrument scoring under 75 tries the alternatives (each other "
                                                "model's split, a half-and-half blend with it, other source parts) and keeps "
                                                "whichever is clearly cleaner and still fits the original.", 1),
            ("mutes", "Mute bars where an instrument is only leftover singing or noise", "Worked out after the fixes.", 1),
            ("acappella", "Turn down the parts where only the singer sings",
             "Careful: only where the instruments are far under the voice and the voice is nearly all of the original, "
             "for 2 seconds or more; turned down 15 dB with smooth fades, not cut.",
             len(self._safe_runs(False))),
            ("acappella_hard", "Be aggressive there",
             "Also turns down shorter and less certain voice-only moments (instruments only 8 dB under the voice). "
             "Off by default: it can cut a soft instrument playing under the voice.",
             len(self._safe_runs(True))),
            ("notch", "Notch the singer's notes where leftover singing remains",
             "After everything else: narrow cuts on exactly the notes being sung (and their reverb), only in the centre of "
             "the mix and only in the stretches where an instrument still moves like the singer.", 1),
            ("tone", "Match the tone under the vocals, across all frequencies",
             "Each instrument under the singing is matched to the same parts where nobody sings, band by band "
             "(replaces the simpler fill-in and mud cut for that instrument).", 1),
            ("seams", "Smooth the seams", "Where the original recording takes over, crossfades are lengthened where the "
                                          "switch would be heard.", 1),
        ]
        return [{"key": k, "title": t, "detail": d, "count": int(c),
                 "on": bool(c) and (k != "bestof" or bool(self.alt_modes())) and k not in self.EXTRA_STEPS,
                 "extra": k in self.EXTRA_STEPS}
                for k, t, d, c in steps]

    def build_best(self, steps=None, progress=lambda f, m: None):
        """Work through every kind of fix in order, choose one where they overlap, check everything against
        the original, and apply the result as ONE undo step. Returns a report.

        Order and rules (why each one is where it is):
          1 keep      original recording where nobody sings: nothing beats it, and kept parts ignore fixes
          2 bestof    other models' splits, bar by bar: the base the rest builds on
          3 versions  a cleaner copy of a whole repeating part (close matches only)
          4 bars      repeated riffs, where no best version covers them
          5 hits      blended clean hits/note starts. On drums they replace ordinary repairs in that part;
                      on pitched instruments ordinary repairs win, and note starts fill only parts with none
          6 repairs   close matches only, where nothing above covers that instrument
          7 leftovers leftover singing removed; 8 clicks fixed
          9 mutes     bars that are only ghost vocal or noise (measured on the result)
         10 tone      lift what's still thin and cut the mud under the vocals (measured on the result)
        Anything that doesn't fit the original (wrong part, off the beat) is never applied; applied ones
        that don't fit are taken out."""
        steps = set(steps or self.AUTO_STEPS)
        with self.lock:
            if self.stage != "detected":
                raise RuntimeError("Find damage first (step 3), then build.")
            h0 = len(self.history)
            before = set(self.suggestions)
            snap = self._snapshot(list(self.suggestions))
            mixer0 = json.loads(json.dumps(self.mixer))
            report = []
            ov = lambda a, b: a["start"] < b["end"] - 0.01 and b["start"] < a["end"] - 0.01

            # -- start fresh: take back what the last build chose and set (your own decisions stay as they are),
            #    so building again never stacks on top of the last build
            for x in self.suggestions.values():
                if x.pop("by_build", False):
                    x["status"] = x.pop("by_build_was", "pending")
                    x.pop("skip_reason", None)
            m = self.mixer
            m["keep"] = [r for r in m.get("keep", []) if not r.get("auto")]
            m["mute_ranges"] = [r for r in m.get("mute_ranges", []) if not (r.get("auto") or r.get("acappella"))]
            m["eq_ranges"] = [r for r in m.get("eq_ranges", []) if not r.get("auto")]
            for k_, cv in (m.pop("build_tone", None) or {}).items():
                cur = m.get("tone_eq", {}).get(k_)
                if cur is not None and len(cur) == len(cv):
                    left = [round(float(a_) - float(b_), 2) for a_, b_ in zip(cur, cv)]
                    if any(abs(v) >= 0.05 for v in left):
                        m["tone_eq"][k_] = left
                    else:
                        m["tone_eq"].pop(k_, None)
            m["seams"] = []
            self._render_cache.clear()
            q_start = None
            try:
                q_start = self.quality()["now"]
            except Exception as e:
                log(f"build: starting score skipped: {e}")

            # -- make the candidates (all still waiting)
            progress(0.05, "Finding the cleanest versions of repeating parts")
            if "bestof" in steps and self.alt_modes() and not any(i.startswith("e_") for i in self.suggestions):
                try:
                    self.best_of_models()
                except Exception as e:
                    log(f"build: best-of skipped: {e}")
            if "versions" in steps:
                for x in self.best_versions():
                    if not x["status"]:
                        try:
                            self.make_take(x["stem"], x["section"], x["best"]["section"])
                        except Exception as e:
                            log(f"build: best version skipped: {e}")
            if "bars" in steps:
                for g in self.best_bars():
                    if not g["status"]:
                        try:
                            self.make_bars(g["stem"], g["best"], g["damaged"], g["members"],
                                           blend=len(g["members"]) - len(g["damaged"]) >= 3)
                        except Exception as e:
                            log(f"build: repeated bars skipped: {e}")
            progress(0.25, "Rebuilding hits and note starts")
            if "hits" in steps:
                for hh in self.best_hits():
                    if not hh["status"]:
                        try:
                            self.make_hits(hh["stem"], hh["sound"], blend=hh.get("blend_n", 0) >= 3)
                        except Exception as e:
                            log(f"build: hits skipped: {e}")
            if "notes" in steps:
                progress(0.32, "Rebuilding damaged notes")
                try:
                    self.make_note_repairs()
                except Exception as e:
                    log(f"build: note repairs skipped: {e}")
            if "loops" in steps:
                progress(0.34, "Playing the loops again from clean copies")
                try:
                    self.make_pattern_rebuilds()
                except Exception as e:
                    log(f"build: loop rebuild skipped: {e}")
            if "smartclean" in steps:
                progress(0.36, "Learning what belongs on each track")
                try:
                    self.smart_clean()
                except Exception as e:
                    log(f"build: smart cleanup skipped: {e}")
            if "pattern" in steps:
                progress(0.35, "Learning the drum pattern")
                try:
                    self.make_pattern_hits()
                except Exception as e:
                    log(f"build: drum pattern skipped: {e}")
            del self.history[h0:]              # one undo step for all of it (recorded below)

            # -- check everything against the original
            progress(0.45, "Checking every fix against the original")
            cand = [i for i, x in self.suggestions.items()
                    if x["type"] in self.CHECK_TYPES and x["status"] in ("pending", "accepted", "auto", "low")]
            self.check_ids(cand)
            bad = {i for i in cand if self.flagged(i)}

            # -- choose
            keep = [{"start": sc["start"], "end": sc["end"], "mode": "original", "auto": True}
                    for sc in self._free_sections()] if "keep" in steps else []
            n_free = len(keep)
            gap_keep = []
            if "gaps" in steps:
                try:
                    gap_keep = [{"start": a_, "end": b_, "mode": "original", "auto": True, "gap": True}
                                for a_, b_ in self.vocal_gaps()
                                if not any(r["start"] <= a_ and r["end"] >= b_ for r in keep)]
                except Exception as e:
                    log(f"build: vocal gaps skipped: {e}")
            keep = keep + gap_keep
            kept = self.keep_ranges("original") + keep
            inside_kept = lambda x: any(r["start"] <= x["start"] + 0.05 and r["end"] >= x["end"] - 0.05 for r in kept)
            accept, reject = set(), set()
            for i in bad:                                   # never applied; taken out if they were
                if self.suggestions[i]["status"] in ("accepted", "auto"):
                    reject.add(i)
            S = self.suggestions
            on = lambda i: (i in accept) or (S[i]["status"] in ("accepted", "auto") and i not in reject)
            usable = lambda i: i not in bad and not inside_kept(S[i]) and S[i]["status"] in ("pending", "accepted", "auto")

            def pick(kind, cond=lambda x: True):
                for i, x in S.items():
                    if x["type"] == kind and usable(i) and cond(x) and S[i]["status"] == "pending":
                        accept.add(i)
            if "refs" in steps:
                pick("alt", lambda x: x.get("alt_mode") == "imported")
            if "loops" in steps:
                pick("alt", lambda x: x.get("alt_mode") == "pattern" and (x.get("check") or {}).get("u", 1) < 0.2)
            if "notes" in steps:
                pick("alt", lambda x: x.get("alt_mode") == "notes")
            if "bestof" in steps:
                pick("alt", lambda x: x.get("alt_mode") == "best-of" and self.ref_track(x["stem"]) is None)
            if "versions" in steps:
                pick("take", lambda x: x.get("conf", 0) >= 0.92 and (x.get("check") or {}).get("u", 1) < 0.15)
            def active(kind):
                """Applied fixes of one kind, by instrument (looked up once, not once per fix)."""
                d = {}
                for j, y in S.items():
                    if y["type"] == kind and on(j):
                        d.setdefault(y["stem"], []).append(y)
                return d
            takes_on = active("take")
            for k_, ys in active("alt").items():          # your clean tracks cover what they replace
                takes_on.setdefault(k_, []).extend(y for y in ys if y.get("alt_mode") == "imported")
            covered = lambda x: any(ov(y, x) for y in takes_on.get(x["stem"], ()))
            if "bars" in steps:
                pick("bars", lambda x: not covered(x) and (x.get("check") or {}).get("u", 1) < 0.15)
            drum = lambda x: x["stem"] in self.PERCUSSIVE
            if "hits" in steps:
                pick("hits", lambda x: drum(x) and not x.get("pattern") and not covered(x))
            if "pattern" in steps:
                pick("hits", lambda x: x.get("pattern") and not covered(x))
            if "repairs" in steps:
                hits_on = active("hits")
                drum_hits = lambda x: drum(x) and any(ov(y, x) for y in hits_on.get(x["stem"], ()))
                pick("repair", lambda x: x.get("recommended") and not covered(x) and not drum_hits(x))
            if "hits" in steps:                              # note starts only where no repair fills that part
                reps_on = active("repair")
                pick("hits", lambda x: not drum(x) and not x.get("pattern") and not covered(x)
                     and not any(ov(y, x) for y in reps_on.get(x["stem"], ())))
            if "recover" in steps:
                pick("rebuild", lambda x: x.get("recover"))
            if "leftovers" in steps:
                for i, x in S.items():
                    if x["type"] in ("bleed", "ess") and x["status"] == "pending" and not inside_kept(x) and not x.get("smart"):
                        accept.add(i)
            if "smartclean" in steps:
                for i, x in S.items():
                    if x.get("smart") and x["status"] == "pending" and not inside_kept(x):
                        accept.add(i)
            if "clicks" in steps:
                for i, x in S.items():
                    if x["type"] == "glitch" and x["status"] == "pending":
                        accept.add(i)

            # -- apply statuses and the kept parts, then measure the result for mutes and tone
            progress(0.6, "Applying the chosen fixes")
            def mark(i, status, why=None):
                x = S[i]
                if not x.get("by_build"):
                    x["by_build_was"] = x["status"]
                x["by_build"] = True
                x["status"] = status
                if why:
                    x["skip_reason"] = why
            for i in accept:
                mark(i, "accepted")
            for i in reject:
                mark(i, "rejected", "Doesn't line up with the original")
            if keep:
                self.mixer["keep"] = self._clean_keep(self.mixer.get("keep", []) + keep)
            self._render_cache.clear()
            mutes, lift, cut, improved, toned = [], {}, {}, [], {}
            if "improve" in steps:
                try:
                    improved, _ = self.improve_weak(75, lambda f, m: progress(0.62 + 0.06 * f, m))
                except Exception as e:
                    log(f"build: improving weak sections skipped: {e}")
                self._render_cache.clear()
            if "mutes" in steps:
                progress(0.7, "Looking for bars that are only leftover singing")
                for k in self.instruments:
                    try:
                        mutes += [{"start": r["start"], "end": r["end"], "stem": k, "auto": True} for r in self.ghost_runs(k)]
                    except Exception as e:
                        log(f"build: mute check skipped for {k}: {e}")
                if mutes:
                    self.mixer["mute_ranges"] = self.mixer.get("mute_ranges", []) + mutes
                    self._render_cache.clear()
            acap = []
            if "acappella" in steps or "acappella_hard" in steps:
                progress(0.72, "Finding the parts where only the singer sings")
                try:
                    kept_r = self.keep_ranges("original") + keep
                    acap = [{"start": round(a_, 3), "end": round(b_, 3), "stem": None, "acappella": True}
                            for a_, b_ in self.vocal_only_runs("acappella_hard" in steps)
                            if not any(r["start"] <= a_ and r["end"] >= b_ for r in kept_r)]
                    self.mixer["mute_ranges"] = [r for r in self.mixer.get("mute_ranges", []) if not r.get("acappella")] + acap
                    self._render_cache.clear()
                except Exception as e:
                    log(f"build: a cappella check skipped: {e}")
            notched = 0
            if "notch" in steps:
                progress(0.78, "Finding where leftover singing remains")
                have = self.mixer.setdefault("eq_ranges", [])
                for k in [x for x in self.instruments if x not in NOT_REPAIRED]:
                    try:
                        for a_, b_ in self.leftover_runs(k):
                            if any(r.get("auto") and r.get("stem") == k and r["start"] <= a_ and r["end"] >= b_ for r in have):
                                continue
                            have.append({"start": round(a_, 2), "end": round(b_, 2), "stem": k, "mode": "pitch", "db": -9,
                                         "only_vocals": True, "tails": True, "centre": True, "preset": "vocal", "auto": True})
                            notched += 1
                    except Exception as e:
                        log(f"build: leftover map skipped for {k}: {e}")
                if notched:
                    self._render_cache.clear()
            if "tone" in steps:
                progress(0.8, "Matching the tone under the vocals")
                for k in [x for x in self.instruments if x not in NOT_REPAIRED]:
                    try:
                        old_lift = self.mixer.get("vocal_lift", {}).pop(k, None)
                        old_cut = self.mixer.get("mud_cut", {}).pop(k, None)
                        curve = self.tone_curve(k)
                        if curve is None or not np.any(np.abs(curve) >= 1.0):
                            if old_lift is not None:            # nothing measurable: leave what was there
                                self.mixer["vocal_lift"][k] = old_lift
                            if old_cut is not None:
                                self.mixer["mud_cut"][k] = old_cut
                            continue
                        prev = np.asarray(self.mixer.get("tone_eq", {}).get(k) or np.zeros(len(curve)), float)
                        eq = np.clip(prev + curve, -8, 8)
                        self.mixer.setdefault("tone_eq", {})[k] = [round(float(v), 2) for v in eq]
                        self.mixer.setdefault("build_tone", {})[k] = [round(float(v), 2) for v in eq - prev]
                        toned[k] = (float(eq.max()), float(eq.min()))
                    except Exception as e:
                        log(f"build: tone match skipped for {k}: {e}")
            if "seams" in steps and self.keep_ranges("original"):
                progress(0.86, "Smoothing the seams")
                try:
                    self.mixer["seams"] = self.find_seams()
                    for k in self.instruments:
                        self.__dict__.get("_metric_cache", {}).pop(("toned", k), None)
                except Exception as e:
                    log(f"build: seams skipped: {e}")

            # -- final check: nothing applied may disagree with the original
            progress(0.9, "Checking the result against the original")
            applied = [i for i, x in S.items() if x["type"] in self.CHECK_TYPES and x["status"] in ("accepted", "auto")]
            late = [i for i in self.check_ids(applied)]
            for i in late:
                mark(i, "rejected", "Doesn't line up with the original")

            # -- never worse: where the build made a part score lower than before it, its fixes there come out
            worse = []
            if q_start:
                progress(0.92, "Making sure every part sounds better than before")
                try:
                    self._render_cache.clear()
                    q_now = self.quality()["now"]
                    for si_, sc_ in q_now.items():
                        if si_ in q_start and sc_ < q_start[si_] - 2:
                            sec = self.sections[int(si_)]
                            back = [i for i, x in S.items() if x.get("by_build") and x["status"] == "accepted"
                                    and x["type"] in self.CHECK_TYPES and ov(x, sec)]
                            for i in back:
                                mark(i, "rejected", "Made this part sound worse")
                            if back:
                                worse.append(sec["name"])
                    if worse:
                        self._render_cache.clear()
                except Exception as e:
                    log(f"build: score check skipped: {e}")

            # -- settle everything else: options the build didn't use are put aside (with the reason), so Review
            #    holds the finished result, not a pile of alternatives that contradict it
            fix_types = set(self.CHECK_TYPES) | {"bleed", "ess", "glitch"}
            on_by_stem = {}
            for j, y in S.items():
                if y["status"] in ("accepted", "auto") and y["type"] in fix_types:
                    on_by_stem.setdefault(y["stem"], []).append(y)
            settled = 0
            for i, x in S.items():
                if x["status"] != "pending" or x["type"] not in fix_types:
                    continue
                if self.flagged(i):
                    why = "Doesn't line up with the original"
                elif inside_kept(x) or any(r["start"] <= x["start"] + 0.05 and r["end"] >= x["end"] - 0.05 for r in keep):
                    why = "The original recording is used here"
                elif any(ov(y, x) for y in on_by_stem.get(x["stem"], ())):
                    why = "Another fix was used here"
                else:
                    why = "Not a close enough match to use"
                mark(i, "rejected", why)
                settled += 1

            # -- one undo step, refreshed displays
            new = [i for i in self.suggestions if i not in before]
            self.history.append({"label": "Build the best instrumental",
                                 "items": snap + [{"id": i, "sug": None, "stem": S[i]["stem"], "patch": None} for i in new],
                                 "mixer": mixer0})
            self.history = self.history[-200:]
            self._render_cache.clear()
            for k in self.instruments:
                self._refresh_after(k, 0, self.stems[k].shape[1])
            self._compute_grid()
            self.meta["verified"] = {"at": time.time(), "applied": self._applied_count(), "checked": len(applied),
                                     "flagged": len(late)}

            head = []
            try:
                qf = self.quality()
                b_, n_ = qf.get("before", {}), qf.get("now", {})
                if n_:
                    avg = lambda d: sum(d.values()) / max(1, len(d))
                    head.append(f"Clean score: {avg(b_):.0f} for the split as it came, {avg(n_):.0f} now (out of 100)")
            except Exception as e:
                log(f"build: final score skipped: {e}")
            kept_all = self.keep_ranges("original")
            if kept_all:
                tot = sum(r["end"] - r["start"] for r in kept_all)
                dur = max(1e-6, float(self.meta.get("duration") or 1))
                bits = [f"{n_free} whole part{'s' if n_free != 1 else ''} without singing"] if n_free else []
                if gap_keep:
                    bits.append(f"{len(gap_keep)} gap{'s' if len(gap_keep) != 1 else ''} between the vocal lines")
                head.append(f"The original recording is used for {tot:.0f} s ({100 * tot / dur:.0f}% of the song)"
                            + (f": {' and '.join(bits)}" if bits else ""))
            report[:0] = head
            n_by = {}
            for i in accept:
                if S[i]["status"] == "accepted":
                    n_by[S[i]["type"]] = n_by.get(S[i]["type"], 0) + 1
            names = {"alt": ("best-of-models take", "best-of-models takes"), "take": ("best version", "best versions"),
                     "bars": ("repeated-bar fix", "repeated-bar fixes"), "hits": ("hit/note rebuild", "hit/note rebuilds"),
                     "repair": ("repair", "repairs"), "bleed": ("leftover-singing cleanup", "leftover-singing cleanups"),
                     "ess": ("\"s\" sound removal", "\"s\" sound removals"), "glitch": ("click fix", "click fixes"),
                     "rebuild": ("recovered or rebuilt part", "recovered or rebuilt parts")}
            for t, n in n_by.items():
                one, many = names.get(t, (t, t + "s"))
                report.append(f"Applied {n} {one if n == 1 else many}")
            if mutes:
                report.append(f"Muted {len(mutes)} stretch{'es' if len(mutes) != 1 else ''} that were only leftover singing or noise")
            if acap:
                tot = sum(r["end"] - r["start"] for r in acap)
                report.append(f"Silenced {len(acap)} part{'s' if len(acap) != 1 else ''} where only the singer sings "
                              f"({tot:.1f} s in all{', aggressive' if 'acappella_hard' in steps else ''}); listed under Muted parts")
            report += improved
            if notched:
                report.append(f"Notched the singer's notes in {notched} stretch{'es' if notched != 1 else ''} where leftover "
                              f"singing remained (centre only, -9 dB); listed in the Levels tab")
            for k, (hi_, lo_) in toned.items():
                report.append(f"Matched the tone of the {k} under the vocals (from {lo_:+.1f} to {hi_:+.1f} dB across the range)")
            sm = [x for x in self.mixer.get("seams", []) if x.get("fade", 0) > 0.05]
            if "seams" in steps and sm:
                report.append(f"Lengthened {len(sm)} crossfade{'s' if len(sm) != 1 else ''} where the original recording takes over")
            if worse:
                report.append(f"Took the fixes back out of {', '.join(worse)}: they made {'it' if len(worse) == 1 else 'those parts'} "
                              f"score lower than before")
            misfit = len(reject) + len(late)
            if misfit:
                report.append(f"Left out {misfit} fix{'es' if misfit != 1 else ''} that don't line up with the original")
            if settled:
                report.append(f"Put aside {settled} other option{'s' if settled != 1 else ''} the build didn't need (another fix "
                              f"or the original covers them, or they weren't close enough); they're under Rejected, "
                              f"each with the reason, if you want to try one")
            return {"report": report, "new": len(new)}

    # ------------------------------------------------------------------ double-check against the original
    CHECK_TYPES = ("repair", "hits", "bars", "take", "alt", "rebuild")
    CHECK_MAX_S = 40.0          # longer edits (whole-song takes) are other models' splits: not checked

    def flagged(self, sid):
        x = self.suggestions.get(sid) or {}
        return bool((x.get("check") or {}).get("flag"))

    def check_fix(self, sid):
        """Check one fix against the original recording. Stores x["check"] and returns the problem
        in words, or None. A fix that doesn't fit loses its Recommended mark."""
        x = self.suggestions.get(sid)
        if not x or x["type"] not in self.CHECK_TYPES or sid not in self.patches or self.original is None:
            return None
        k = x["stem"]
        lo, p = self.patches[sid]
        n = min(self.stems[k].shape[1], self.original.shape[1])
        a, b = max(0, lo), min(n, lo + p.shape[1])
        if b - a < int(0.05 * self.sr) or (b - a) > self.CHECK_MAX_S * self.sr:
            return None
        P = p[:, a - lo:b - lo].astype(np.float32)
        ref = self.ref_track(k) if x.get("alt_mode") != "imported" else None
        against = np.asarray(ref[:, a:b], np.float32) if ref is not None and ref.shape[1] >= b else self.original[:, a:b]
        r = VF.check_region(self.stems[k][:, a:b], P, against, self.sr)
        flag = None
        swap = x["type"] in ("take", "bars") or (x["type"] == "alt" and x.get("alt_mode") != "imported")
        if r["u"] > (0.3 if ref is not None else 0.2 if swap else 0.35):    # swaps must match the original closely
            flag = (f"Adds sound your clean {k} track doesn't have here ({r['u']:.0%} of what it adds). Try another "
                    f"source, or reject it." if ref is not None else
                    f"Adds sound the original doesn't have here ({r['u']:.0%} of what it adds), so it is probably "
                    f"a different part, note or instrument. Try another source, or reject it.")
        elif r["offset_ms"]:
            ms = r["offset_ms"]
            flag = (f"What it adds lands about {abs(ms)} ms {'early' if ms > 0 else 'late'} against the original's "
                    f"beat, which can make hits flam. Try another source, or reject it.")
        x["check"] = {"u": round(r["u"], 3), "offset_ms": r["offset_ms"], "flag": flag}
        if flag and x.get("recommended"):
            x["recommended"] = False
        return flag

    def check_ids(self, ids):
        out = []
        for sid in ids or []:
            try:
                if self.check_fix(sid):
                    out.append(sid)
            except Exception as e:
                log(f"check skipped for {sid}: {e}")
        return out

    def grid_drift(self):
        """Bars where the beat the app found sits off the original's attacks (fixes borrowed there are
        the ones most likely to land off the beat)."""
        if self.original is None or len(self.beat_times) < 4:
            return []
        o = VF.onset_env(self.original.mean(0), self.sr)
        imp = np.zeros(len(o), np.float32)
        ms = (np.asarray(self.beat_times) * 1000).astype(int)
        imp[ms[(ms >= 0) & (ms < len(o))]] = 1
        from scipy.ndimage import uniform_filter1d
        imp = uniform_filter1d(imp, 9)
        out = []
        last = len(self.beat_times) - 1
        for s0, L in self.windows:
            t0 = max(0, int(self.beat_times[s0] * 1000) - 60)
            t1 = min(len(o), int(self.beat_times[min(s0 + L, last)] * 1000) + 60)
            if t1 - t0 < 300:
                continue
            sh, ratio = VF.best_offset(imp[t0:t1], o[t0:t1], 40)
            if abs(sh) >= 15 and ratio >= 1.3:
                out.append({"start": float(self.beat_times[s0]), "end": float(self.beat_times[min(s0 + L, last)]), "ms": int(sh)})
        return out

    def verify_all(self):
        """Double-check every fix that's applied or waiting against the original, and the beat grid."""
        with self.lock:
            ids = [i for i, x in self.suggestions.items()
                   if x["type"] in self.CHECK_TYPES and x["status"] in ("pending", "accepted", "auto", "low")]
            flagged = self.check_ids(ids)
            try:
                drift = self.grid_drift()
            except Exception as e:
                log(f"beat check skipped: {e}")
                drift = []
            self.meta["grid_drift"] = drift
            self.meta["verified"] = {"at": time.time(), "applied": self._applied_count(), "checked": len(ids),
                                     "flagged": len(flagged)}
            return flagged

    # ------------------------------------------------------------------ best of several models, bar by bar
    def _alt_track(self, alt, k, n, cur):
        def take(name):
            x = np.asarray(alt[name][:, :n], np.float32)
            return np.pad(x, ((0, 0), (0, n - x.shape[1])))
        if k in alt:
            return take(k)
        if k == "other" and "other" in alt:
            return take("other") + sum((take(x) for x in ("guitar", "piano") if x in alt and x not in cur),
                                       np.zeros((2, n), np.float32))
        return None

    def _bar_frames(self, wi):
        s0, L = self.windows[wi]
        last = len(self.beat_times) - 1
        return (int(self.beat_times[s0] * self.sr / HOP), int(self.beat_times[min(s0 + L, last)] * self.sr / HOP),
                int(self.beat_times[s0] * self.sr), int(self.beat_times[min(s0 + L, last)] * self.sr))

    def _vocal_frames(self, nf):
        vc = self._vocal_curve(0, nf * HOP)[::HOP][:nf]
        return np.pad(vc, (0, nf - len(vc))) > 0.3

    def best_of_models(self):
        """For each instrument, score every bar of the current split and of every other model's split
        (kept from "Try another model") for sound the original doesn't have, leftover singing and missing
        level, and build the instrument from whichever is cleanest in each bar. One review item per
        instrument; nothing is applied."""
        with self.lock:
            modes = [m for m in self.alt_modes()]
            if not modes:
                raise RuntimeError("Split the song with another model first: drag across any part, choose "
                                   "Try another model here, and pick a model. Then try this again.")
            from scipy.ndimage import uniform_filter1d
            n = self.original.shape[1]
            Eo = VF.band_energy(self.original.mean(0), self.B)
            Ev = VF.band_energy(self.stems["vocals"].mean(0), self.B)
            nf = Eo.shape[1]
            vf = self._vocal_frames(nf)
            vb = (np.asarray(self.centers) >= 250) & (np.asarray(self.centers) <= 5000)
            cur = [k for k in self.instruments if k not in NOT_REPAIRED]
            ramp = max(3, int(0.02 * self.sr))
            ids = []
            for k in cur:
                cands = {"current": None}
                for m in modes:
                    try:
                        y = self._alt_track(self.load_alt(m), k, n, cur)
                    except Exception as e:
                        log(f"best-of: {m} unreadable: {e}")
                        y = None
                    if y is not None:
                        cands[m] = y
                if len(cands) < 2:
                    continue
                now = self.render_stem(k)
                E = {c: VF.band_energy((now if c == "current" else y).mean(0), self.B) for c, y in cands.items()}
                masks = {c: np.zeros(n, np.float32) for c in cands if c != "current"}
                used = {c: 0 for c in cands}
                for wi in range(len(self.windows)):
                    f0, f1, a, b = self._bar_frames(wi)
                    f1 = min(f1, nf)
                    if f1 - f0 < 4 or b <= a:
                        continue
                    sc, lv = {}, {}
                    for c in cands:
                        u, bl = VF.clean_terms(E[c][:, f0:f1], Eo[:, f0:f1], Ev[:, f0:f1], vf[f0:f1], vb)
                        sc[c] = 0.8 * u + 0.7 * max(0.0, bl - 0.25)
                        lv[c] = 10 * np.log10(float(E[c][:, f0:f1].sum()) + 1e-12)
                    top = max(lv.values())
                    for c in cands:
                        sc[c] += 0.5 * float(np.clip((top - lv[c]) / 10, 0, 1))   # don't win by losing the part
                    best = min(sc, key=sc.get)
                    if best != "current" and sc["current"] - sc[best] < 0.05:
                        best = "current"                       # only switch for a clear win
                    used[best] += 1
                    if best != "current":
                        masks[best][a:min(b, n)] = 1
                if not any(used[c] for c in masks):
                    continue
                W = np.zeros(n, np.float32)
                Y = np.zeros((2, n), np.float32)
                for c, mk in masks.items():
                    if not mk.any():
                        continue
                    mk = uniform_filter1d(mk, ramp)
                    W += mk
                    Y += mk[None, :] * cands[c]
                nz = np.flatnonzero(W > 1e-4)
                lo, hi = int(nz[0]), int(nz[-1]) + 1
                raw = self.stems[k][:, lo:hi]
                sid = f"e_{k}"
                summary = ", ".join(f"{c} in {used[c]} bar{'s' if used[c] != 1 else ''}" for c in cands if used[c])
                self.suggestions[sid] = {
                    "id": sid, "type": "alt", "stem": k, "status": "pending", "start": lo / self.sr, "end": hi / self.sr,
                    "section": self._section_of(lo / self.sr), "conf": 1.0, "severity": 0.0, "strength": 1.0,
                    "donors": [], "alt_mode": "best-of", "blend": 0.0,
                    "note": f"Builds the {k} bar by bar from whichever split is cleanest there ({summary})"}
                self.patches[sid] = (lo, (Y[:, lo:hi] - W[None, lo:hi] * raw).astype(np.float16))
                self.take_w[sid] = (lo, W[lo:hi].astype(np.float16))
                ids.append(sid)
            return ids

    # ------------------------------------------------------------------ how clean each section is
    def quality(self):
        """0-100 clean score per section (and per instrument in it), for the raw split and as it is now.
        Measures sound the original doesn't have (artifacts, wrong parts) and leftover singing."""
        with self.lock:
            if self.stage != "detected" or self.original is None:
                raise RuntimeError("Score sections after step 3, Find damage.")
            Eo = VF.band_energy(self.original.mean(0), self.B)
            Ev = VF.band_energy(self.stems["vocals"].mean(0), self.B)
            nf = Eo.shape[1]
            vf = self._vocal_frames(nf)
            vb = (np.asarray(self.centers) >= 250) & (np.asarray(self.centers) <= 5000)
            n = self.original.shape[1]

            def score(version):
                E = {}
                for k in self.instruments:
                    y = self.stems[k] if version == "raw" else self.shaped_stem(k) * self._gain_curve(k, 0, self.stems[k].shape[1])
                    E[k] = VF.band_energy(np.asarray(y, np.float32).mean(0)[:n], self.B)[:, :nf]
                secs, cells = {}, {}
                for si, sec in enumerate(self.sections):
                    f0, f1 = int(sec["start"] * self.sr / HOP), min(nf, int(sec["end"] * self.sr / HOP))
                    if f1 - f0 < 4:
                        continue
                    tot, acc = 0.0, 0.0
                    for k, Ek in E.items():
                        e = Ek[:, f0:f1]
                        w = float(e.sum())
                        if w <= 1e-6:
                            continue
                        q = VF.clean_score(*VF.clean_terms(e, Eo[:, f0:f1], Ev[:, f0:f1], vf[f0:f1], vb))
                        cells[f"{si}:{k}"] = q
                        tot += w
                        acc += w * q
                    if tot:
                        secs[str(si)] = int(round(acc / tot))
                return secs, cells

            if "quality_before" not in self.meta:
                self.meta["quality_before"] = score("raw")[0]
            now, cells = score("repaired")
            # bar by bar, for the bars with singing: the weakest ones, so one bad bar isn't averaged away
            weak, bar_scores = [], {}
            for k in self.instruments:
                if k in NOT_REPAIRED:
                    continue
                y = self.shaped_stem(k) * self._gain_curve(k, 0, self.stems[k].shape[1])
                Ek = VF.band_energy(np.asarray(y, np.float32).mean(0)[:n], self.B)[:, :nf]
                for wi, (s0, L) in enumerate(self.windows):
                    if not L or float(np.mean(self.VA[s0:s0 + L])) < 0.2:
                        continue
                    a_, b_ = self._window_samples(s0, L)
                    f0_, f1_ = a_ // HOP, min(nf, b_ // HOP)
                    e = Ek[:, f0_:f1_]
                    if f1_ - f0_ < 8 or float(e.sum()) < 1e-6:
                        continue
                    qv = VF.clean_score(*VF.clean_terms(e, Eo[:, f0_:f1_], Ev[:, f0_:f1_], vf[f0_:f1_], vb))
                    weak.append({"stem": k, "start": a_ / self.sr, "end": b_ / self.sr, "score": int(qv)})
                    bar_scores.setdefault(k, []).append([round(a_ / self.sr, 2), round(b_ / self.sr, 2), int(qv)])
            weak.sort(key=lambda x: x["score"])
            self.meta["quality"] = {"before": self.meta["quality_before"], "now": now, "cells": cells,
                                    "weak_bars": [x for x in weak if x["score"] < 70][:15], "bar_scores": bar_scores,
                                    "at": time.time(), "applied": self._applied_count()}
            return self.meta["quality"]

    def ghost_runs(self, k, lazy=False):
        """Stretches of bars where instrument k is best muted: under the singing it is quiet next to how
        it plays where nobody sings, and what's left either rises and falls with the singer (leftover
        vocal), was flagged as leftover vocal, or is so quiet it's only separation noise.
        Returns [{start, end, bars, why}], skipping parts already muted."""
        if self.stage != "detected" or k not in self.stems or k in NOT_REPAIRED:
            return []
        marks = tuple(sorted(i for i, x in self.suggestions.items()
                             if x["stem"] == k and x["type"] in ("bleed", "ess") and x["status"] != "rejected"))
        extra = (json.dumps(self.mixer.get("mute_ranges", []), sort_keys=True), hash(marks))
        return self._stem_cached("ghost", k, extra, lambda: self._ghost_runs(k), lazy)

    def _ghost_runs(self, k):
        lv = self._beat_levels(self.render_stem(k))
        vl = self._beat_levels(self.stems["vocals"])
        va = np.asarray(self.VA)
        nb = min(len(lv), len(vl), len(va), len(self.beat_times) - 1)
        if nb < 8:
            return []
        lv, vl, va = lv[:nb], vl[:nb], va[:nb]
        free = (va < 0.04) & (lv > lv.max() - 60)          # nobody singing, and not digital silence
        if free.sum() < 4:
            return []
        ref = float(np.percentile(lv[free], 75))            # how loud it plays when it plays on its own
        bleed_bars = {x.get("window") for x in self.suggestions.values()
                      if x["type"] in ("bleed", "ess") and x["stem"] == k and x["status"] != "rejected"}
        mutes = self.mixer.get("mute_ranges", [])
        flags = []
        for wi, (s0, L) in enumerate(self.windows):
            e = min(s0 + L, nb)
            if e - s0 < 2 or float(va[s0:e].mean()) < 0.3:
                flags.append(None)
                continue
            level = float(np.median(lv[s0:e]))
            # correlation over the bar and its neighbours: one bar of beats is too few to judge
            c0, c1 = max(0, s0 - L), min(nb, e + L)
            a_, b_ = lv[c0:c1], vl[c0:c1]
            follows = bool(a_.std() > 0.5 and b_.std() > 0.5 and np.corrcoef(a_, b_)[0, 1] > 0.6)
            if level < ref - 24:
                flags.append("noise")
            elif level < ref - 12 and (follows or wi in bleed_bars):
                flags.append("ghost")
            else:
                flags.append(None)
        runs, cur = [], None
        for wi, f in enumerate(flags + [None]):
            if f and cur:
                cur["end_w"] = wi
                cur["why"].add(f)
            elif f:
                cur = {"start_w": wi, "end_w": wi, "why": {f}}
            elif cur:
                runs.append(cur)
                cur = None
        out = []
        for r in runs:
            s0 = self.windows[r["start_w"]][0]
            e0, eL = self.windows[r["end_w"]]
            t0, t1 = float(self.beat_times[s0]), float(self.beat_times[min(e0 + eL, nb)])
            bars = r["end_w"] - r["start_w"] + 1
            if bars < 2 and "ghost" not in r["why"]:
                continue                                      # a single quiet bar isn't worth a mute
            if any(m.get("stem") in (None, k) and m["start"] <= t0 + 0.05 and m["end"] >= t1 - 0.05 for m in mutes):
                continue
            out.append({"start": t0, "end": t1, "bars": bars,
                        "why": "ghost" if "ghost" in r["why"] else "noise"})
        return out

    def tips(self):
        """What to do next: ONE step that fits where the song is, plus at most two extras that truly apply.
        (Build the best instrumental does all the individual fixes, so they aren't suggested one by one.)"""
        out = []
        stage = self.stage
        built = bool(self.meta.get("verified"))
        if stage == "imported":
            return [{"id": "split", "title": "Split it and build the instrumental",
                     "detail": "Splits the song into instruments, finds what the singing damaged and builds the cleanest "
                               "instrumental in one go (Split + build in step 2).",
                     "button": "Split + build", "action": {"kind": "click", "el": "#btnSplitAuto"}}]
        if stage == "split":
            return [{"id": "detect", "title": "Find the damage, then build",
                     "detail": "Step 3 finds the spots the singing damaged; then Build the best instrumental fixes them.",
                     "button": "Find damage", "action": {"kind": "detect"}}]
        if not built:
            out.append({"id": "build", "title": "Build the best instrumental",
                        "detail": "Works through every kind of fix, picks one where they overlap, checks each against the "
                                  "original and applies it all as one step you can undo.",
                        "button": "Build it", "action": {"kind": "auto"}})
        else:
            q = (self.meta.get("quality") or {}).get("now") or {}
            if q:
                si, sc = min(((int(k), v) for k, v in q.items()), key=lambda kv: kv[1])
                sec = self.sections[si] if si < len(self.sections) else None
                if sec is not None and sc < 85:
                    out.append({"id": "listen", "title": f"Listen to {sec['name']}: the part the app is least sure about",
                                "detail": f"Clean score {sc} of 100. Play it with Repaired selected (hold B to hear the split); "
                                          f"if something sounds off, reject its fixes in Review.",
                                "button": "Show it", "action": {"kind": "scope", "section": si}})
            out.append({"id": "export", "title": "Export your instrumental",
                        "detail": "Save the instrumental, the instruments on their own, and samples if you like.",
                        "button": "Export…", "action": {"kind": "click", "el": "#btnExport"}})
        # extras, only when they apply
        cells = getattr(self, "grid", None) or {}
        refs = self.meta.get("refs") or {}
        try:
            per_stem = {}
            for key, c in (cells.items() if isinstance(cells, dict) else []):
                k = key.split(":", 1)[1] if ":" in key else None
                if k and c.get("count"):
                    per_stem.setdefault(k, []).append(c.get("damage", 0))
            weak = [k for k, v in per_stem.items() if np.mean(v) > 0.15 and k not in refs]
            if weak:
                out.append({"id": "stems", "title": f"Have a clean {weak[0]} track? Add it",
                            "detail": f"The {weak[0]} lost the most to the singing. A clean stem, loop or sample from this "
                                      f"song fixes it far better than any repair (drag it onto the window).",
                            "button": "Add clean stems…", "action": {"kind": "click", "el": "#btnRefOpen2"}})
        except Exception:
            pass
        return out[:3]

    def _safe(self, fn, default):
        try:
            return fn()
        except Exception as e:
            import traceback
            log(f"{getattr(fn, '__name__', 'part')} failed, skipped: {e}\n{traceback.format_exc()}")
            return default

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
            "tips": self._safe(self.tips, []),
            "best_versions": self._safe(self.best_versions, []),
            "best_hits": self._safe(lambda: self.best_hits(lazy=True), []),
            "ref_lanes": self._safe(self.ref_lanes, {}),
            "best_bars": self._safe(lambda: [{**g, "damaged_times": [self.beat_times[self.windows[v][0]] for v in g["damaged"]]}
                                             for g in self.best_bars()], []),
            "user_decisions": sum(1 for x in self.suggestions.values() if x["status"] in ("accepted", "rejected")),
            "applied_count": self._safe(self._applied_count, 0),
            "leftover_map": self._safe(lambda: self.leftover_map(lazy=True), {}),
            "map_rate": self.MAP_RATE,
            "warming": self.warming,
        }
