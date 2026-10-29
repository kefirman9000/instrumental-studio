"""Source separation with Demucs (runs on your machine; GPU used automatically if present)."""
import glob
import os
import re
import subprocess
import sys

import numpy as np

from .audio import load_audio, log

MODELS = {
    # key: (label, demucs model names, stems)
    "4stem": ("Drums / Bass / Other (best quality)", ["htdemucs_ft"], ["drums", "bass", "other", "vocals"]),
    "6stem": ("Drums / Bass / Guitar / Piano / Other", ["htdemucs_6s"], ["drums", "bass", "guitar", "piano", "other", "vocals"]),
    "4stem_fast": ("Drums / Bass / Other (fast)", ["htdemucs"], ["drums", "bass", "other", "vocals"]),
    "ensemble": ("Ensemble of two models (least damage, slowest)", ["htdemucs_ft", "htdemucs"],
                 ["drums", "bass", "other", "vocals"]),
}


def demucs_available():
    try:
        import demucs  # noqa: F401
        return True
    except Exception:
        return False


def device_info():
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda", torch.cuda.get_device_name(0)
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps", "Apple GPU"
    except Exception:
        pass
    return "cpu", "CPU (slow — expect several minutes per song)"


def _run_demucs(path, model, outdir, device, progress, frac0, frac1):
    cmd = [sys.executable, "-m", "demucs", "-n", model, "-o", outdir, "-d", device, "--float32", path]
    log("Running: " + " ".join(cmd))
    p = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    tail = []
    for line in p.stderr:
        tail = (tail + [line])[-20:]
        for m in re.finditer(r"(\d+)%\|", line):
            pct = int(m.group(1)) / 100.0
            progress(frac0 + (frac1 - frac0) * pct, f"Separating with {model}")
    p.wait()
    if p.returncode != 0:
        raise RuntimeError("Demucs failed:\n" + "".join(tail))
    hits = glob.glob(os.path.join(outdir, model, "*", "vocals.wav"))
    if not hits:
        raise RuntimeError("Demucs finished but no stems were found.")
    return os.path.dirname(hits[0])


def separate(path, workdir, mode="4stem", progress=lambda f, m: None):
    """Return dict stem_name -> (2, n) float32 array, and the sample rate."""
    if not demucs_available():
        raise RuntimeError("Demucs is not installed. Run:  pip install demucs   (then restart the app)")
    label, models, stems = MODELS[mode]
    device, _ = device_info()
    results = []
    for i, model in enumerate(models):
        a, b = i / len(models), (i + 1) / len(models)
        d = _run_demucs(path, model, os.path.join(workdir, "demucs"), device, progress, a * 0.95, b * 0.95)
        results.append(d)
    out, sr = {}, 44100
    for stem in stems:
        arrs = [load_audio(os.path.join(d, stem + ".wav"), sr)[0] for d in results]
        n = min(a.shape[1] for a in arrs)
        out[stem] = np.mean([a[:, :n] for a in arrs], axis=0).astype(np.float32)
    return out, sr


def load_stem_folder(folder, sr=44100):
    """Use stems from any separator (UVR, Spleeter, LALAL...). Needs a file with 'vocal' in its name;
    every other audio file becomes an instrument stem named after the file."""
    files = [f for f in sorted(os.listdir(folder))
             if os.path.splitext(f)[1].lower() in (".wav", ".flac", ".mp3", ".m4a", ".ogg", ".aiff", ".aif")]
    stems = {}
    for f in files:
        name = os.path.splitext(f)[0].lower()
        words = re.findall(r"[a-z]+", name)
        if any(w in name for w in ("no_vocal", "novocal", "no vocal", "instrumental", "karaoke")) or "inst" in words:
            key = "instrumental"
        elif "vocal" in name or "vox" in words:
            key = "vocals"
        else:
            known = [w for w in ("drums", "drum", "bass", "guitar", "piano", "keys", "synth", "strings", "other")
                     if w in words]
            key = (known[-1] if known else re.sub(r"[^a-z0-9]+", "_", name).strip("_") or "stem")
            key = {"drum": "drums", "keys": "piano"}.get(key, key)
        while key in stems:
            key += "_2"
        stems[key] = load_audio(os.path.join(folder, f), sr)[0]
    if "vocals" not in stems:
        raise RuntimeError("No vocal stem found in that folder (need a file with 'vocal' in its name).")
    n = min(a.shape[1] for a in stems.values())
    return {k: v[:, :n] for k, v in stems.items()}, sr
