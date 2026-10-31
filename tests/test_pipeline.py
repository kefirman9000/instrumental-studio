"""Smoke test of the whole pipeline on the built-in demo song (no separation model needed).

Runs every step and feature end to end and fails on any error, so a broken release never ships:
    python tests/test_pipeline.py
Runs in GitHub Actions before every release (see .github/workflows/release.yml).
"""
import json
import os
import sys
import tempfile
import time
import traceback

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("INSTRUMENTAL_STUDIO_HOME", tempfile.mkdtemp(prefix="is-home-"))

from studio.demo import make_demo  # noqa: E402
from studio.project import Project  # noqa: E402

FAILED = []


def step(name, fn):
    t = time.time()
    try:
        out = fn()
        print(f"  ok   {name} ({time.time() - t:.1f}s)")
        return out
    except Exception:
        FAILED.append(name)
        print(f"  FAIL {name}\n" + "".join("       " + l for l in traceback.format_exc().splitlines(True)))
        return None


def main():
    root = tempfile.mkdtemp(prefix="is-test-")
    damaged, original, clean = make_demo()
    p = Project(os.path.join(root, "demo"))
    p.meta["name"] = "Demo"
    step("set stems", lambda: p.set_stems(damaged, 44100, original, source="demo"))
    step("prepare (map the song)", lambda: p.prepare(lambda f, m: None))
    step("find damage", lambda: p.detect(lambda f, m: None))
    step("overview", lambda: json.dumps(p.overview(), default=str))
    step("render repaired mix", lambda: p.mix("repaired"))
    plan = step("build plan", p.build_plan)
    steps = [x["key"] for x in (plan or []) if x["key"] != "deep"]
    rep = step("build the best instrumental", lambda: p.build_best(steps, lambda f, m: None))
    if rep:
        for line in rep["report"]:
            print("       - " + line)
    step("render repaired mix after build", lambda: p.mix("repaired"))
    tgt = p._recover_target()
    if tgt:
        step("recover details from the vocal track", lambda: p.add_recovered(clean[tgt][:, :p.original.shape[1]] * 0.3))
    k_ref = next((k for k in p.instruments if k in clean and k not in p.PERCUSSIVE), None)
    if k_ref:
        # a clean track of one instrument, starting a little late, as an official stem with silence trimmed would
        trim = int(1.3 * p.sr)
        ids = step("import a clean track (whole track)", lambda: p.add_reference_track(clean[k_ref][:, trim:], k_ref, "clean.wav", "all"))
        print(f"       {len(ids or [])} parts")
        ids = step("import a clean track (only where damaged)", lambda: p.add_reference_track(clean[k_ref], k_ref, "clean.wav"))
        print(f"       {len(ids or [])} parts")
        def checked():
            p.set_status(ids, "accepted", "test")
            others = [i for i, x in p.suggestions.items() if x["stem"] == k_ref and i not in ids]
            p.check_ids(others)
            return sum(1 for i in others if p.flagged(i))
        r = step("check other fixes against the clean track", checked)
        print(f"       {r} flagged")
    def ident():
        k = next(k for k in p.instruments if k in clean)
        got = p.identify_track(clean[k])
        assert got[0] == k, f"identified {got} for {k}"
        return got
    print(f"       {step('auto-detect a clean track', ident)}")
    k_loop = next((k for k in p.instruments if k in clean and k not in p.PERCUSSIVE), None)
    if k_loop:
        def loop():
            s0, L = next(w for w in p.windows if w[1] == max(x[1] for x in p.windows))
            a, b = p._window_samples(s0 + 8, L * 2) if s0 + 8 + 2 * L < len(p.beats) else p._window_samples(s0, L * 2)
            return p.add_reference_loop(clean[k_loop][:, a:b], "loop.wav", "vocals")
        r = step("place a clean loop where it plays", loop)
        print(f"       {r and (len(r[0]), r[1])}")
    drum = next((k for k in p.instruments if k in p.PERCUSSIVE), None)
    if drum:
        def sample():
            sd = p._sounds(drum)
            if not sd["sounds"]:
                return "no repeated sounds"
            snd = sd["sounds"][0]
            q = int(sd["pos"][snd["best"]])
            try:
                return p.add_reference_sample(clean[drum][:, q:q + int(0.3 * p.sr)], "hit.wav")
            except ValueError as e:
                return f"no match ({e})"
        r = step("import a clean sample", sample)
        print(f"       {r if isinstance(r, str) else (len(r[0]), r[1])}")
    step("render after imports", lambda: p.mix("repaired"))
    n = p.original.shape[1]
    dur = n / p.sr
    for mode in ("fixed", "follow", "pitch"):
        def eq(mode=mode):
            p.set_mixer({"eq_ranges": [{"start": 0.0, "end": dur, "stem": None, "mode": mode, "db": -9,
                                        "only_vocals": True, "tails": True, "centre": True, "preset": "vocal"}]})
            y = p.mix("repaired")
            assert np.all(np.isfinite(y)), "non-finite audio"
        step(f"lower frequencies ({mode})", eq)
    def acap():
        return len(p.vocal_only_runs(False)), len(p.vocal_only_runs(True))
    print(f"       {step('find the a cappella parts (careful, aggressive)', acap)}")
    step("seams", p.find_seams)
    step("quality scores", p.quality)
    step("leftover map", lambda: p.leftover_map(lazy=False))
    step("drum pattern", p.make_pattern_hits)
    def blend_bars():
        gs = p.best_bars()
        if not gs:
            return "no repeated bars"
        g = gs[0]
        return p.make_bars(g["stem"], g["best"], g["damaged"], g["members"], blend=True)
    step("blend repeated bars", blend_bars)
    step("find repeating samples", lambda: [len(p.find_loops(p.render_stem(k), drums=k in p.PERCUSSIVE)) for k in p.instruments])
    step("undo", p.undo)
    step("save and load", lambda: (p.save(), Project.load(p.root)))
    out = os.path.join(root, "export")
    files = step("export everything", lambda: p.export(out, "wav", ("mix", "stems", "raw", "vocals", "report", "tempo",
                                                                     "drum_midi", "samples_raw", "samples_merged"),
                                                       match_level=True, declick=True))
    if files:
        print(f"       {len(files)} files")
    def loud():
        p.export(os.path.join(root, "export_lufs"), "wav", ("mix",), lufs=-14.0)
        got = p.meta.get("last_export_lufs")
        assert got is not None and got <= -13.5, got
        return got
    print(f"       {step('export at streaming loudness', loud)} LUFS")
    step("vinyl declick on the stems", lambda: p.declick_stems("normal"))
    print()
    if FAILED:
        print(f"{len(FAILED)} step(s) failed: {', '.join(FAILED)}")
        sys.exit(1)
    print("All steps passed.")


if __name__ == "__main__":
    main()
