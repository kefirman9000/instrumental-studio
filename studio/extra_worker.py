"""Runs one of the optional extra separation models in its own process.

    python -m studio.extra_worker roformer MODEL_FILE INPUT.wav OUTDIR MODELS_DIR
        -> OUTDIR/vocals.wav, OUTDIR/instrumental.wav       (audio-separator, Roformer models)
    python -m studio.extra_worker drumsep INPUT.wav OUTDIR MODELS_DIR
        -> OUTDIR/kick.wav, snare.wav, toms.wav, cymbals.wav (mdxnet-infer, DrumSep)
"""
import glob
import os
import re
import shutil
import subprocess
import sys

import numpy as np
from scipy.io import wavfile


def _read(path):
    rate, x = wavfile.read(path)
    if np.issubdtype(x.dtype, np.integer):
        x = x.astype(np.float32) / float(np.iinfo(x.dtype).max + 1)
    x = x.astype(np.float32)
    return rate, (x[:, None] if x.ndim == 1 else x)


def roformer(model, inp, outdir, models_dir):
    from audio_separator.separator import Separator
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(models_dir, exist_ok=True)
    sep = Separator(output_dir=outdir, output_format="WAV", model_file_dir=models_dir)
    sep.load_model(model_filename=model)
    files = sep.separate(inp)
    found = {}
    for f in files:
        path = f if os.path.isabs(f) else os.path.join(outdir, os.path.basename(f))
        name = os.path.basename(path).lower()
        if "(instrumental)" in name or "(no vocals)" in name or "(no_vocals)" in name:
            found["instrumental"] = path
        elif "(vocals)" in name:
            found["vocals"] = path
    if set(found) != {"vocals", "instrumental"}:
        raise SystemExit("Unexpected output from the model: " + ", ".join(os.path.basename(f) for f in files))
    for k, path in found.items():
        shutil.move(path, os.path.join(outdir, k + ".wav"))
    print("DONE vocals instrumental", flush=True)


DRUM_PARTS = {"kick": ["kick"], "snare": ["snare"], "toms": ["tom"],
              "cymbals": ["hh", "hihat", "hi-hat", "hi_hat", "ride", "crash", "cymbal"]}


def drumsep(inp, outdir, models_dir):
    os.makedirs(outdir, exist_ok=True)
    os.environ.setdefault("MDXNET_INFER_CACHE", models_dir)
    raw = os.path.join(outdir, "raw")
    try:
        from mdxnet_infer import separate
        separate(inp, output_dir=raw, model_name="drumsep-6stem")
    except ImportError:
        exe = shutil.which("mdxnet-infer") or os.path.join(os.path.dirname(sys.executable), "mdxnet-infer")
        r = subprocess.run([exe, inp, "-o", raw, "--model", "drumsep-6stem"])
        if r.returncode:
            raise SystemExit("DrumSep failed")
    files = glob.glob(os.path.join(raw, "**", "*.wav"), recursive=True)
    rate, total = None, {}
    for f in files:
        name = re.sub(r"[^a-z]", "", os.path.basename(f).lower().rsplit(".", 1)[0][-12:])
        part = next((p for p, keys in DRUM_PARTS.items() if any(k.replace("-", "").replace("_", "") in name for k in keys)), None)
        if part is None:
            continue
        rate, x = _read(f)
        total[part] = x if part not in total else total[part][:len(x)] + x[:len(total[part])]
    if len(total) < 2:
        raise SystemExit("DrumSep produced no usable parts: " + ", ".join(os.path.basename(f) for f in files))
    for part, x in total.items():
        wavfile.write(os.path.join(outdir, part + ".wav"), rate, x.astype(np.float32))
    shutil.rmtree(raw, ignore_errors=True)
    print("DONE " + " ".join(sorted(total)), flush=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "roformer":
        roformer(a[1], a[2], a[3], a[4])
    elif a[0] == "drumsep":
        drumsep(a[1], a[2], a[3])
    else:
        raise SystemExit("unknown job " + a[0])
