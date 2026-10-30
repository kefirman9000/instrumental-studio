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
        ids = step("import a clean track", lambda: p.add_reference_track(clean[k_ref][:, trim:], k_ref, "clean.wav"))
        print(f"       {len(ids or [])} parts")
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
    step("seams", p.find_seams)
    step("quality scores", p.quality)
    step("leftover map", lambda: p.leftover_map(lazy=False))
    step("drum pattern", p.make_pattern_hits)
    step("undo", p.undo)
    step("save and load", lambda: (p.save(), Project.load(p.root)))
    out = os.path.join(root, "export")
    files = step("export everything", lambda: p.export(out, "wav", ("mix", "stems", "raw", "vocals", "report", "tempo",
                                                                     "drum_midi", "samples_raw", "samples_merged"),
                                                       match_level=True, declick=True))
    if files:
        print(f"       {len(files)} files")
    step("vinyl declick on the stems", lambda: p.declick_stems("normal"))
    print()
    if FAILED:
        print(f"{len(FAILED)} step(s) failed: {', '.join(FAILED)}")
        sys.exit(1)
    print("All steps passed.")


if __name__ == "__main__":
    main()
