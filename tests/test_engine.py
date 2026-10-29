"""Ground-truth test: synthetic song -> simulated separator damage -> analyse + repair -> compare."""
import os
import sys
import time
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from studio.demo import make_demo
from studio.project import Project
from studio.analysis import band_matrix
from studio.audio import stft, EPS


def metrics(est, true, B, mask_frames):
    E = np.abs(stft(est.mean(0)))[:, mask_frames]
    T = np.abs(stft(true.mean(0)))[:, mask_frames]
    missing = float((np.clip(T - E, 0, None) ** 2).sum() / ((T ** 2).sum() + EPS))
    extra = float((np.clip(E - T, 0, None) ** 2).sum() / ((T ** 2).sum() + EPS))
    be, bt = 10 * np.log10(B @ E ** 2 + 1e-9), 10 * np.log10(B @ T ** 2 + 1e-9)
    act = bt > bt.max() - 60
    lsd = float(np.sqrt(((be - bt) ** 2)[act].mean()))
    return missing, extra, lsd


def main():
    t = time.time()
    damaged, original, truth = make_demo()
    print(f"synth+damage {time.time()-t:.1f}s, {damaged['vocals'].shape[1]/44100:.1f}s of audio")
    root = tempfile.mkdtemp()
    p = Project(os.path.join(root, "demo"))
    p.set_stems(damaged, 44100, original, source="demo")
    t = time.time()
    p.analyze(lambda f, m: None)
    print(f"analysis {time.time()-t:.1f}s  bpm={p.meta['bpm']:.1f}  beats={len(p.beats)}")
    print("sections:", [(s["name"], round(s["start"], 1), round(s["end"], 1)) for s in p.sections])
    sug = list(p.suggestions.values())
    from collections import Counter
    print("suggestions:", Counter((s["type"], s["status"]) for s in sug))
    B, _ = band_matrix(44100, 40)
    nfr = damaged["vocals"].shape[1] // 512 + 1
    va_frames = np.zeros(nfr, bool)
    for j, v in enumerate(p.VA):
        if v > 0.08:
            va_frames[p.beats[j]:p.beats[j + 1]] = True
    print(f"\n{'stem':8s} {'missing% dmg→rep':>18s} {'extra% dmg→rep':>16s} {'LSD dB dmg→rep':>16s}  untouched-max")
    for k in p.instruments:
        rep = p.render_stem(k)
        m0 = metrics(damaged[k], truth[k], B, va_frames)
        m1 = metrics(rep, truth[k], B, va_frames)
        quiet = ~va_frames
        qs = np.repeat(quiet, 512)[:rep.shape[1]]
        diff = np.abs(rep - damaged[k])[:, qs[:rep.shape[1]]].max() if qs.any() else 0
        print(f"{k:8s} {100*m0[0]:7.1f} → {100*m1[0]:5.1f}   {100*m0[1]:6.1f} → {100*m1[1]:5.1f}   "
              f"{m0[2]:6.2f} → {m1[2]:5.2f}   {diff:.4f}")
    print("\ngrid (section: stem state/repairability):")
    for si, s in enumerate(p.sections):
        print(f"  {s['name']:14s}", "  ".join(f"{k}:{p.grid[f'{si}:{k}']['state'][:6]}/{p.grid[f'{si}:{k}']['repairability']:.2f}"
                                       for k in p.instruments))
    bleed = [s for s in sug if s["type"] == "bleed"]
    print(f"\nbleed suggestions: {len(bleed)} -> stems {Counter(s['stem'] for s in bleed)}, "
          f"times {sorted(round(s['start']) for s in bleed)[:12]}")
    # API paths
    sid = next(s["id"] for s in sug if s["type"] == "repair" and s["status"] == "auto")
    for mode in ("before", "after", "patch", "donor"):
        assert p.preview(sid, mode).shape[0] == 2
    p.set_strength(sid, 0.5); p.set_status([sid], "rejected"); p.undo(); p.undo()
    cands = p.donor_candidates(sid)
    p.set_donor(sid, beat=cands[-1]["beat"])
    p.set_mixer({"stem_gain": {"drums": -3}, "section_gain": {"1:bass": 4}})
    p.save()
    q = Project.load(p.root)
    assert len(q.suggestions) == len(p.suggestions)
    files = p.export(os.path.join(root, "export"), "wav")
    print("export:", [os.path.basename(f) for f in files])
    ov = q.overview()
    print("overview ok, cols", ov["display"]["cols"])


if __name__ == "__main__":
    main()
