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
    # Roformer takes the vocals out first (cleanest vocal removal), then Demucs splits what's left
    "hybrid": ("4 tracks, cleanest vocal removal (Roformer + Demucs)", ["htdemucs_ft"],
               ["drums", "bass", "other", "vocals"]),
    "hybrid6": ("6 tracks, cleanest vocal removal (Roformer + Demucs)", ["htdemucs_6s"],
                ["drums", "bass", "guitar", "piano", "other", "vocals"]),
}
NEEDS_EXTRA = {"hybrid": "roformer", "hybrid6": "roformer"}
BS_ROFORMER = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
KARAOKE = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"


def models_dir():
    from .server import HOME
    return os.path.join(HOME, "models")


def _run_extra(args, progress, frac0, frac1, label):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [sys.executable, "-m", "studio.extra_worker"] + args
    log("Running: " + " ".join(cmd))
    progress(frac0, label + " (the model downloads the first time)")
    p = proc.popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    for chunk in proc.lines(p, what=label):
        for line in re.split(r"[\r\n]", chunk):
            if not line.strip():
                continue
            tail = (tail + [line])[-20:]
            m = re.search(r"(\d+)%\|", line)
            if m:
                progress(frac0 + (frac1 - frac0) * int(m.group(1)) / 100.0, label)
    p.wait()
    if p.returncode != 0:
        raise RuntimeError(label + " failed:\n" + "\n".join(tail[-12:]))


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
        r = proc.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=45)
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
    for chunk in proc.lines(p, what=f"Splitting with {model}"):
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


def separate(path, workdir, mode="4stem", progress=lambda f, m: None, options=None):
    """Return dict stem_name -> (2, n) float32 array, and the sample rate."""
    if not demucs_available():
        raise RuntimeError("The separation model isn't installed yet.")
    from .installer import extra_installed
    need = {NEEDS_EXTRA.get(mode)} | ({"roformer"} if (options or {}).get("backing") else set()) \
        | ({"drumsep"} if (options or {}).get("drums") else set())
    for key in need - {None}:
        if not extra_installed(key):
            raise RuntimeError(f"need_extra:{key}")
    from scipy.io import wavfile
    label, models, stems = MODELS[mode]
    sr = 44100
    os.makedirs(workdir, exist_ok=True)
    audio, _ = load_audio(path, sr)                 # ffmpeg / scipy, not torchaudio
    wav = os.path.join(workdir, "input.wav")
    wavfile.write(wav, sr, audio.T.astype(np.float32))
    options = options or {}
    from scipy.io import wavfile as _wf
    steps = 1 + (mode in NEEDS_EXTRA) + bool(options.get("backing")) + bool(options.get("drums"))
    span = 0.97 / steps
    at = 0.0
    roformer_vocals = None
    demucs_in = wav
    if mode in NEEDS_EXTRA:
        rdir = os.path.join(workdir, "roformer")
        _run_extra(["roformer", BS_ROFORMER, wav, rdir, models_dir()], progress, at, at + span,
                   "Taking out the vocals with Roformer")
        roformer_vocals = load_audio(os.path.join(rdir, "vocals.wav"), sr)[0]
        demucs_in = os.path.join(rdir, "instrumental.wav")
        at += span
    results = []
    for i, model in enumerate(models):
        a, b = at + span * i / len(models), at + span * (i + 1) / len(models)
        results.append(_run_model(demucs_in, model, os.path.join(workdir, model), progress, a, b))
    at += span
    out = {}
    for stem in stems:
        # average every model that produced this track (e.g. drums from both, guitar only from the 6-track one)
        arrs = [load_audio(os.path.join(d, stem + ".wav"), sr)[0] for d in results
                if os.path.exists(os.path.join(d, stem + ".wav"))]
        n = min(a.shape[1] for a in arrs)
        out[stem] = np.mean([a[:, :n] for a in arrs], axis=0).astype(np.float32)
    if roformer_vocals is not None:
        # Demucs ran on the Roformer instrumental: whatever it calls vocals is leftover singing
        n0 = min(roformer_vocals.shape[1], out["vocals"].shape[1])
        out["vocals"] = roformer_vocals[:, :n0] + out["vocals"][:, :n0]
    n = min(a.shape[1] for a in out.values())
    out = {k: v[:, :n] for k, v in out.items()}
    if options.get("backing"):
        vpath = os.path.join(workdir, "vocals_for_karaoke.wav")
        _wf.write(vpath, sr, out["vocals"].T.astype(np.float32))
        kdir = os.path.join(workdir, "karaoke")
        _run_extra(["roformer", KARAOKE, vpath, kdir, models_dir()], progress, at, at + span,
                   "Separating backing vocals from the lead")
        lead = load_audio(os.path.join(kdir, "vocals.wav"), sr)[0][:, :n]
        lead = np.pad(lead, ((0, 0), (0, n - lead.shape[1])))
        out["backing_vocals"] = (out["vocals"] - lead).astype(np.float32)   # keeps everything adding up
        out["vocals"] = lead.astype(np.float32)
        at += span
    if options.get("drums") and "drums" in out:
        dpath = os.path.join(workdir, "drums_for_split.wav")
        _wf.write(dpath, sr, out["drums"].T.astype(np.float32))
        ddir = os.path.join(workdir, "drumsep")
        _run_extra(["drumsep", dpath, ddir, models_dir()], progress, at, at + span,
                   "Splitting the drums into kick, snare, toms and cymbals")
        parts = {}
        for part in ("kick", "snare", "toms", "cymbals"):
            f = os.path.join(ddir, part + ".wav")
            if os.path.exists(f):
                x = load_audio(f, sr)[0][:, :n]
                parts[part] = np.pad(x, ((0, 0), (0, n - x.shape[1])))
        if parts:
            rest = out.pop("drums") - sum(parts.values())
            key = "cymbals" if "cymbals" in parts else next(iter(parts))
            parts[key] = parts[key] + rest              # nothing of the drums gets lost
            out.update({k: v.astype(np.float32) for k, v in parts.items()})
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
