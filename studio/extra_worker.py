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


HEADROOM = 0.5     # the level the song is handed to the model at


def _fit_back(x, v, i):
    """Gains (a, b) so a*vocals + b*instrumental adds back up to the song as closely as possible (least squares,
    all channels). The model's own level changes are undone this way even when they were not a plain peak scale."""
    xv, xi = v.reshape(-1).astype(np.float64), i.reshape(-1).astype(np.float64)
    y = x.reshape(-1).astype(np.float64)
    M = np.array([[xv @ xv, xv @ xi], [xv @ xi, xi @ xi]]) + 1e-9 * np.eye(2)
    a, b = np.linalg.solve(M, np.array([xv @ y, xi @ y]))
    return float(a), float(b)


def roformer(model, inp, outdir, models_dir):
    """Roformer through audio-separator, which (whatever its settings allow) turns a song peaking over 0.9 down
    before separating it, turns each result down again on its own, and saves them as 16-bit files. Vocals and
    instrumental then no longer add back up to the song: every place the app puts original and separated audio
    side by side jumped in level, and the instrumental kept about a tenth of the singing. So the song goes in at
    half level (none of that kicks in), comes back at its own level, and both parts are matched so they add up
    to the song again."""
    from audio_separator.separator import Separator
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(models_dir, exist_ok=True)
    rate, x = _read(inp)
    peak = float(np.abs(x).max()) or 1.0
    scale = HEADROOM / peak
    work = os.path.join(outdir, "_input_headroom.wav")
    wavfile.write(work, rate, (x * scale).astype(np.float32))
    sep = Separator(output_dir=outdir, output_format="WAV", model_file_dir=models_dir,
                    normalization_threshold=1.0, sample_rate=rate)
    sep.load_model(model_filename=model)
    files = sep.separate(work)
    os.remove(work)
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
    parts = {}
    for k, path in found.items():
        _, y = _read(path)
        os.remove(path)
        parts[k] = y / scale
    n = min(len(x), *(len(y) for y in parts.values()))
    v, i = parts["vocals"][:n], parts["instrumental"][:n]
    a, b = _fit_back(x[:n], v, i)
    if 0.7 < a < 1.4 and 0.7 < b < 1.4:          # a plain level fix; anything else would be a model problem
        v, i = v * a, i * b
    print(f"level match: vocals x{a:.3f}, instrumental x{b:.3f}", flush=True)
    for k, y in (("vocals", v), ("instrumental", i)):
        wavfile.write(os.path.join(outdir, k + ".wav"), rate, y.astype(np.float32))
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
