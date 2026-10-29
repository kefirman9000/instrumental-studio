"""A song project: stems, analysis, suggestions (repairs / bleed cleanup), levels, undo, save, export."""
import json
import os
import shutil
import threading
import time

import numpy as np

from . import analysis as A
from .audio import EPS, HOP, N_FFT, load_audio, save_audio, stft, log
from .repair import match_window, refine_lag, repair_window, bleed_window, MARGIN, XFADE

DISPLAY_BANDS = 96
DS = 4                      # display columns = analysis frames / DS  (~21.5 columns per second)
STEM_ORDER = ["drums", "bass", "guitar", "piano", "other", "instrumental"]
STEM_COLORS = {"drums": "#E8875B", "bass": "#5B8DEF", "guitar": "#4FB286", "piano": "#C46BD6",
               "other": "#D9B44A", "instrumental": "#5BC0D9", "vocals": "#E05C7A"}

DEFAULTS = {
    "auto_conf": 0.6,          # confidence at/above which repairs are applied automatically
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
        self.mixer = {"stem_gain": {}, "mute": {}, "solo": {}, "section_gain": {}}
        self._render_cache = {}
        self.display = {}

    # ------------------------------------------------------------------ creation
    @property
    def instruments(self):
        names = [k for k in self.stems if k != "vocals"]
        return sorted(names, key=lambda k: (STEM_ORDER.index(k) if k in STEM_ORDER else 99, k))

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
        os.makedirs(os.path.join(self.root, "stems"), exist_ok=True)
        for k, v in self.stems.items():
            np.save(os.path.join(self.root, "stems", k + ".npy"), v)
        np.save(os.path.join(self.root, "stems", "_original.npy"), self.original)

    # ------------------------------------------------------------------ analysis
    def analyze(self, progress=lambda f, m: None):
        with self.lock:
            self._analyze(progress)

    def _analyze(self, progress):
        sr, st = self.sr, self.meta["settings"]
        inst_names = self.instruments
        voc = self.stems["vocals"]
        inst_sum = sum(self.stems[k] for k in inst_names)
        self.inst_sum = inst_sum
        progress(0.02, "Measuring where the vocals were")
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

        progress(0.08, "Finding the beat")
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

        progress(0.14, "Detecting song sections")
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
            progress(0.16 + 0.12 * i / (len(inst_names) + 1), f"Analysing {k}")
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
        ref = max(float(v.max()) for v in self.display["raw"].values())
        self.display["ref_db"] = ref
        del r_bins

        # suggestions
        self.suggestions, self.patches = {}, {}
        self.level_floor = {}
        jobs = [(k, w) for k in inst_names for w in range(len(windows))]
        total = max(len(jobs), 1)
        for n_done, (k, wi) in enumerate(jobs):
            if n_done % 8 == 0:
                progress(0.3 + 0.62 * n_done / total, f"Checking {k} for damage")
            self._make_window_suggestions(k, wi)
        progress(0.93, "Scoring sections")
        for k in inst_names:
            self._render_cache.pop(k, None)
            self._refresh_after(k, 0, self.stems[k].shape[1])
        self._compute_grid()
        self.meta["analyzed"] = True
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
        status = "auto" if conf >= st["auto_conf"] else ("pending" if conf >= st["review_conf"] else "low")
        self.suggestions[sid] = {**base, "status": status, "conf": conf, "severity": res["severity"],
                                 "added_db": res["added_db"], "damage_area": res["damage_area"],
                                 "donors": self._donor_info(donors, L), "manual_donor": False}
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
        for sid, sug in self.suggestions.items():
            if sug["stem"] != k or not self._applied(sug) or sid not in self.patches:
                continue
            lo, p = self.patches[sid]
            a, b = max(lo, 0), min(lo + p.shape[1], n)
            if b > a:
                out[:, a:b] += p[:, a - lo:b - lo].astype(np.float32)
        self._render_cache[key] = out
        return out

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
        if version == "original":
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
                    y = self.render_stem(k)[:, a:b] - self.stems[k][:, a:b]
                else:
                    y = self.render_stem(k, version)[:, a:b]
                g = self._gain_curve(k, a, b) if use_mixer else 1.0
                out += y * g
            return out
        return out[:, region[0]:region[1]] if region else out

    def _gain_curve(self, k, a, b):
        m = self.mixer
        g0 = 10 ** (float(m["stem_gain"].get(k, 0.0)) / 20)
        sg = m["section_gain"]
        if not any(key.endswith(":" + k) and v for key, v in sg.items()):
            return g0
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
        return (g0 * curve)[None, :]

    def preview(self, sid, mode="after", context=1.5, solo=True):
        sug = self.suggestions[sid]
        a = int(max(0, (sug["start"] - context) * self.sr))
        b = int(min(self.stems["vocals"].shape[1], (sug["end"] + context) * self.sr))
        k = sug["stem"]
        if mode == "donor" and sug.get("donors"):
            d = sug["donors"][0]
            da = int(max(0, (d["start"] - context) * self.sr))
            src = self.stems[k] if solo else self.mix("raw", use_mixer=False)
            return src[:, da:da + (b - a)]
        if mode == "original":
            return self.original[:, a:b]
        lo, p = self.patches.get(sid, (a, np.zeros((2, 0), np.float16)))
        base = self.stems[k][:, a:b] if solo else self.mix("raw", region=(a, b), use_mixer=False)
        delta = np.zeros_like(base)
        pa, pb = max(lo, a), min(lo + p.shape[1], b)
        if pb > pa:
            delta[:, pa - a:pb - a] = p[:, pa - lo:pb - lo].astype(np.float32)
        if mode == "before":
            return base
        if mode == "patch":
            return delta
        return base + delta

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
            for k in {self.suggestions[i]["stem"] for i in ids}:
                self._render_cache.pop(("stem", k), None)
            for i in ids:
                self._invalidate(self.suggestions[i]["stem"], i)
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
            else:
                s0, s1 = self._window_samples(sug["beat"], sug["beats"])
                b = bleed_window(self.stems[sug["stem"]], self.stems["vocals"], s0, s1, strength)
                if b is not None:
                    self.patches[sid] = (b["lo"], b["patch"].astype(np.float16))
            if sug["status"] in ("pending", "low"):
                sug["status"] = "accepted"
            self._invalidate(sug["stem"], sid)
            self._compute_grid()

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
                        "severity": res["severity"], "added_db": res["added_db"], "status": "accepted",
                        "type": "repair", "note": "Donor chosen by you"})
            self._store_patch(sid, sug["stem"], res)
            self._invalidate(sug["stem"], sid)
            self._compute_grid()

    def set_mixer(self, mixer):
        with self.lock:
            self._push("Levels", [])
            for key in ("stem_gain", "mute", "solo", "section_gain"):
                if key in mixer:
                    self.mixer[key] = mixer[key]
            self._render_cache.pop("mix", None)

    def undo(self):
        with self.lock:
            if not self.history:
                return None
            h = self.history.pop()
            self.mixer = h["mixer"]
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
                y = self.render_stem(k)[:, a:b]
                if k != "vocals" and y.size:
                    y = y * self._gain_curve(k, a, b)
                out[f"{si}:{k}"] = float(20 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-9)) if y.size else -120.0
        return out

    # ------------------------------------------------------------------ bulk + settings
    def bulk(self, action, stem=None, section=None, types=("repair",)):
        ids = [s["id"] for s in self.suggestions.values()
               if (stem is None or s["stem"] == stem) and (section is None or s["section"] == section)
               and s["type"] in types and s["status"] != "nodonor"]
        status = {"accept": "accepted", "reject": "rejected"}.get(action)
        if action == "reset":
            st = self.meta["settings"]
            with self.lock:
                self._push("Reset to suggestions", ids)
                for i in ids:
                    s = self.suggestions[i]
                    s["status"] = "pending" if s["type"] == "bleed" else (
                        "auto" if s["conf"] >= st["auto_conf"] else "pending" if s["conf"] >= st["review_conf"] else "low")
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
                put(f"{name} - {k} (repaired)", self.render_stem(k) * self._gain_curve(k, 0, self.stems[k].shape[1]))
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
        p.inst_sum = sum(p.stems[k] for k in p.instruments)
        p.B, p.centers = A.band_matrix(p.sr, 40)
        p.Bd, _ = A.band_matrix(p.sr, DISPLAY_BANDS, 30, 16000)
        z = np.load(os.path.join(root, "patches.npz"))
        for key in z.files:
            if key.endswith("__p"):
                sid = key[:-3]
                p.patches[sid] = (int(z[sid + "__lo"][0]), z[key])
        if os.path.exists(os.path.join(root, "display.npz")):
            z = np.load(os.path.join(root, "display.npz"))
            n = p.stems["vocals"].shape[1] // HOP + 1
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
                if s["type"] == "repair" and s["status"] in ("auto", "pending", "low") and not s.get("manual_donor"):
                    new = "auto" if s["conf"] >= st["auto_conf"] else "pending" if s["conf"] >= st["review_conf"] else "low"
                    if new != s["status"]:
                        s["status"] = new
                        self._invalidate(s["stem"], s["id"])
            self._compute_grid()

    def rename_section(self, i, name):
        with self.lock:
            self.sections[int(i)]["name"] = str(name)[:40]

    def overview(self):
        return {
            "meta": self.meta, "sections": self.sections, "beats": self.beat_times,
            "vocal_activity": [float(x) for x in self.VA], "stems": self.instruments,
            "colors": STEM_COLORS, "grid": self.grid, "stem_summary": self.stem_summary,
            "suggestions": list(self.suggestions.values()), "mixer": self.mixer,
            "levels": self.section_levels(), "can_undo": bool(self.history),
            "undo_label": self.history[-1]["label"] if self.history else None,
            "display": {"cols": self.display["cols"], "rows": DISPLAY_BANDS, "fps": self.display["fps"]},
        }
