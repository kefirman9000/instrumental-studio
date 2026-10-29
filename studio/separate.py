"""Source separation with Demucs (runs on your machine; GPU used automatically if present)."""
import os
import re
import subprocess

from . import proc  # noqa: E402  (hidden windows on Windows)
import sys

import numpy as np

from .audio import load_audio, log

MODELS = {
    # key: (label, demucs model names, stems)
    "4stem": ("4 tracks: drums, bass, other (best quality)", ["htdemucs_ft"], ["drums", "bass", "other", "vocals"]),
    "4stem_fast": ("4 tracks, fast", ["htdemucs"], ["drums", "bass", "other", "vocals"]),
    "6stem": ("6 tracks: adds guitar and piano", ["htdemucs_6s"],
              ["drums", "bass", "guitar", "piano", "other", "vocals"]),
    "6stem_best": ("6 tracks, best quality (slower)", ["htdemucs_6s", "htdemucs_ft"],
                   ["drums", "bass", "guitar", "piano", "other", "vocals"]),
    "ensemble": ("4 tracks, least damage (two models, slowest)", ["htdemucs_ft", "htdemucs"],
                 ["drums", "bass", "other", "vocals"]),
}


def demucs_available():
    from .installer import installed
    return installed()


_DEVICE = None


def device_info():
    """(device, human description). Probed once in a subprocess so the app stays light."""
    global _DEVICE
    if _DEVICE:
        return _DEVICE
    code = ("import torch;"
            "c=torch.cuda.is_available();"
            "m=getattr(torch.backends,'mps',None) and torch.backends.mps.is_available();"
            "print('cuda|'+torch.cuda.get_device_name(0) if c else 'mps|Apple GPU' if m else 'cpu|CPU')")
    try:
        r = proc.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        dev, name = r.stdout.strip().split("|", 1)
    except Exception:
        dev, name = "cpu", "CPU"
    if dev == "cpu":
        name = "CPU (slow: expect several minutes per song)"
    _DEVICE = (dev, name)
    return _DEVICE


def _run_model(wav_path, model, outdir, progress, frac0, frac1):
    cmd = [sys.executable, "-m", "studio.demucs_worker", model, wav_path, outdir, "auto"]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log("Running: " + " ".join(cmd))
    progress(frac0, f"Loading {model} (downloads once, about 80-300 MB)")
    p = proc.popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail, passes, done_passes, last = [], 1, 0, 0.0
    for chunk in iter(lambda: p.stdout.readline(), ""):
        for line in re.split(r"[\r\n]", chunk):   # tqdm redraws with carriage returns
            if not line.strip():
                continue
            tail = (tail + [line])[-20:]
            if line.startswith("PASSES"):
                passes = max(1, int(line.split()[1]))
                continue
            if line.startswith("FALLBACK"):
                progress(None, "The GPU couldn't run this model; continuing on the CPU")
                continue
            m = re.search(r"(\d+)%\|", line)
            if m:
                pct = int(m.group(1)) / 100.0
                if pct + 0.2 < last:          # a new model in the bag started
                    done_passes = min(done_passes + 1, passes - 1)
                last = pct
                f = (done_passes + pct) / passes
                progress(frac0 + (frac1 - frac0) * f, f"Separating with {model}")
    p.wait()
    if p.returncode != 0 or not os.path.exists(os.path.join(outdir, "vocals.wav")):
        raise RuntimeError("Separation failed:\n" + "\n".join(tail[-12:]))
    return outdir


def separate(path, workdir, mode="4stem", progress=lambda f, m: None):
    """Return dict stem_name -> (2, n) float32 array, and the sample rate."""
    if not demucs_available():
        raise RuntimeError("The separation model isn't installed yet.")
    from scipy.io import wavfile
    label, models, stems = MODELS[mode]
    sr = 44100
    os.makedirs(workdir, exist_ok=True)
    audio, _ = load_audio(path, sr)                 # ffmpeg / scipy, not torchaudio
    wav = os.path.join(workdir, "input.wav")
    wavfile.write(wav, sr, audio.T.astype(np.float32))
    results = []
    for i, model in enumerate(models):
        a, b = i / len(models), (i + 1) / len(models)
        results.append(_run_model(wav, model, os.path.join(workdir, model), progress, a * 0.97, b * 0.97))
    out = {}
    for stem in stems:
        # average every model that produced this track (e.g. drums from both, guitar only from the 6-track one)
        arrs = [load_audio(os.path.join(d, stem + ".wav"), sr)[0] for d in results
                if os.path.exists(os.path.join(d, stem + ".wav"))]
        n = min(a.shape[1] for a in arrs)
        out[stem] = np.mean([a[:, :n] for a in arrs], axis=0).astype(np.float32)
    n = min(a.shape[1] for a in out.values())
    out = {k: v[:, :n] for k, v in out.items()}
    if "guitar" in out and len(results) > 1:
        # keep the tracks adding up to the song: whatever the averaged tracks no longer cover goes to "other"
        mix = load_audio(wav, sr)[0][:, :n]
        out["other"] = (mix - sum(v for k, v in out.items() if k != "other")).astype(np.float32)
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
