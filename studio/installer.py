"""Installs the separation model (Demucs + PyTorch) into the Python that runs the app.

- NVIDIA GPU (Windows/Linux): installs the CUDA build of PyTorch that matches the driver.
- Mac: the standard build, which uses the Apple GPU.
- Otherwise: a CPU build (on Linux the small CPU-only one, not the 2.5 GB CUDA default).

Demucs itself is installed without its pinned dependencies (its requirements pin an old
torchaudio that has no wheels for current Python), and its model code is used directly by
studio/demucs_worker.py, so torchaudio's file I/O is never needed.
"""
import importlib
import importlib.util
import os
import re
import shutil
import subprocess

from . import proc  # noqa: E402  (hidden windows on Windows)
import sys

TORCH_INDEX = "https://download.pytorch.org/whl/"
DEMUCS_DEPS = ["dora-search", "einops", "julius>=0.2.3", "openunmix", "pyyaml", "tqdm", "omegaconf"]


def installed():
    importlib.invalidate_caches()
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "torchaudio", "demucs", "openunmix", "dora"))


def nvidia_cuda():
    """CUDA version supported by the NVIDIA driver, e.g. (12, 8), or None."""
    exe = shutil.which("nvidia-smi")
    if not exe and sys.platform.startswith("win"):
        for p in (r"C:\Windows\System32\nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if os.path.exists(p):
                exe = p
                break
    if not exe:
        return None
    try:
        out = proc.run([exe], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def plan():
    """What will be installed, in words, how big it is, and the torch index (None = default PyPI)."""
    p = _plan()
    big = bool(p["index"] and "cu1" in p["index"]) or (sys.platform.startswith("linux") and not p["index"])
    p["size"] = "about 3 GB" if big else "about 600 MB"
    p["runs_on"] = p["gpu"] or "your computer's processor (CPU)"
    return p


def _plan():
    if sys.platform == "darwin":
        return {"gpu": "Apple GPU" if _apple_silicon() else None, "index": None}
    cuda = nvidia_cuda()
    if cuda:
        for need, tag in (((13, 0), "cu130"), ((12, 8), "cu128"), ((12, 6), "cu126")):
            if cuda >= need:
                return {"gpu": f"NVIDIA GPU (CUDA {cuda[0]}.{cuda[1]})", "index": TORCH_INDEX + tag}
        return {"gpu": None, "index": TORCH_INDEX + "cpu",
                "note": "Your NVIDIA driver is too old for GPU separation; update it and reinstall to use the GPU."}
    if sys.platform.startswith("linux"):
        return {"gpu": None, "index": TORCH_INDEX + "cpu"}
    return {"gpu": None, "index": None}


def _apple_silicon():
    import platform
    return platform.machine() == "arm64"


def _pip(args, progress, lo, hi, label):
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "raw"]
    cmd = base + args
    child = proc.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail, cur = [], label
    for line in child.stdout:
        tail = (tail + [line])[-30:]
        m = re.match(r"\s*Progress (\d+) of (\d+)", line)
        if m and int(m.group(2)) > 0:
            frac = int(m.group(1)) / int(m.group(2))
            progress(lo + (hi - lo) * frac * 0.9, cur)
            continue
        m = re.match(r"\s*(Downloading|Collecting|Installing collected packages)\s*:?\s*(\S+)?", line)
        if m:
            name = (m.group(2) or "").split("/")[-1].split("-")[0]
            cur = f"{label}: {m.group(1).lower()} {name}".strip()
            progress(None, cur)
    child.wait()
    text = "".join(tail)
    if child.returncode == 0:
        progress(hi, label)
        return
    low = text.lower()
    if "--progress-bar" in low or ("invalid choice" in low and "raw" in low):
        # older pip without raw progress output: run again without it
        r = proc.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check"] + args,
                           capture_output=True, text=True)
        if r.returncode == 0:
            progress(hi, label)
            return
        text = (r.stdout + r.stderr)[-1500:]
    if "externally-managed-environment" in text:
        raise RuntimeError(
            "This Python is managed by your system and won't accept new packages. Install Python from "
            "python.org (or make a virtual environment), then start the app with that Python.")
    if "No space left" in text:
        raise RuntimeError("Not enough disk space. The separation model needs about 3 GB free.")
    if any(k in text for k in ("Retrying", "NewConnectionError", "ProxyError", "ConnectionError",
                                "Temporary failure in name resolution", "403 Forbidden", "timed out")):
        raise RuntimeError("Couldn't download the model files. Check your internet connection, then try again.")
    if "Could not find a version" in text or "No matching distribution" in text:
        raise RuntimeError("PyTorch isn't available for this version of Python yet. Python 3.11, 3.12 or 3.13 "
                           f"from python.org works. (You have {sys.version.split()[0]}.)")
    raise RuntimeError(f"Installing failed while {label.lower()}:\n" + text[-800:])


def install(progress=lambda f, m: None):
    """Install everything. progress(fraction or None, message)."""
    p = plan()
    where = p["gpu"] or "your CPU"
    torch_args = ["torch", "torchaudio"] + (["--index-url", p["index"]] if p["index"] else [])
    progress(0.01, f"Setting up for {where}")
    _pip(torch_args, progress, 0.02, 0.80, "Installing PyTorch")
    _pip(DEMUCS_DEPS, progress, 0.80, 0.92, "Installing helpers")
    _pip(["--no-deps", "demucs"], progress, 0.92, 0.97, "Installing Demucs")
    progress(0.98, "Checking the installation")
    r = proc.run([sys.executable, "-c",
                        "import torch, demucs.pretrained, demucs.apply;"
                        "print('cuda' if torch.cuda.is_available() else 'mps' if getattr(torch.backends,'mps',None) "
                        "and torch.backends.mps.is_available() else 'cpu')"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Installed, but it doesn't load:\n" + r.stderr[-600:])
    importlib.invalidate_caches()
    dev = r.stdout.strip()
    progress(1.0, "Separation model installed")
    return {"device": dev, "note": p.get("note")}


# ----------------------------------------------------------------------------- optional extra models
EXTRAS = {
    "roformer": {
        "label": "Roformer vocal models",
        "what": "Cleaner vocal removal (BS-Roformer), and separating backing vocals from the lead (karaoke model).",
        "pip": ["audio-separator"], "module": "audio_separator",
        "size": "about 200 MB now, plus each model (a few hundred MB) the first time it's used",
    },
    "drumsep": {
        "label": "DrumSep",
        "what": "Splits the drums into kick, snare, toms and cymbals.",
        "pip": ["mdxnet-infer"], "module": "mdxnet_infer",
        "size": "a small package now, plus the DrumSep model the first time it's used",
    },
}


def extra_installed(key):
    importlib.invalidate_caches()
    return importlib.util.find_spec(EXTRAS[key]["module"]) is not None


def install_extra(key, progress=lambda f, m: None):
    if not installed():
        raise RuntimeError("Install the separation model first; the extras build on it.")
    e = EXTRAS[key]
    progress(0.02, f"Installing {e['label']}")
    # never let an extra swap out the PyTorch build that matches the graphics card
    _pip(e["pip"] + ["--upgrade-strategy", "only-if-needed"], progress, 0.05, 0.95, f"Installing {e['label']}")
    r = proc.run([sys.executable, "-c", f"import {e['module']}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{e['label']} installed but doesn't load:\n" + r.stderr[-500:])
    importlib.invalidate_caches()
    progress(1.0, f"{e['label']} installed")
