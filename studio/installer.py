"""Installs the separation model (Demucs + PyTorch) into the Python that runs the app.

- NVIDIA GPU (Windows/Linux): installs the CUDA build of PyTorch that matches the driver.
- Mac: the standard build, which uses the Apple GPU.
- Otherwise: a CPU build (on Linux the small CPU-only one, not the 2.5 GB CUDA default).

Demucs itself is installed without its pinned dependencies (its requirements pin an old
torchaudio that has no wheels for current Python), and its model code is used directly by
studio/demucs_worker.py, so torchaudio's file I/O is never needed.
"""
import importlib
import time
import importlib.util
import os
import re
import shutil
import subprocess

from . import proc  # noqa: E402  (hidden windows on Windows)
import sys

TORCH_INDEX = "https://download.pytorch.org/whl/"
DEMUCS_DEPS = ["dora-search", "einops", "julius>=0.2.3", "openunmix", "pyyaml", "tqdm", "omegaconf"]


_spec_cache = {}        # module name -> (checked at, found); scanning site-packages is slow on Windows
SPEC_TTL = 30.0


def _has(module, fresh=False):
    """Is a module installed? Asked on every status poll, so the answer is kept for SPEC_TTL seconds:
    re-scanning the disk each time (antivirus inspects every directory read) made polls pile up."""
    now = time.time()
    hit = _spec_cache.get(module)
    if hit and not fresh and now - hit[0] < SPEC_TTL:
        return hit[1]
    importlib.invalidate_caches()
    try:
        found = importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        found = False
    _spec_cache[module] = (now, found)
    return found


def forget_checks():
    """Call after installing something, so the next check looks again."""
    _spec_cache.clear()


def installed(fresh=False):
    return all(_has(m, fresh) for m in ("torch", "torchaudio", "demucs", "openunmix", "dora"))


_gpu_names = None


def gpu_names():
    """Names of the graphics cards in this computer, e.g. ["NVIDIA GeForce RTX 3060"] (asked once)."""
    global _gpu_names
    if _gpu_names is not None:
        return _gpu_names
    names = []
    try:
        if sys.platform.startswith("win"):
            r = proc.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"],
                         capture_output=True, text=True, timeout=30)
            names = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        elif sys.platform == "darwin":
            r = proc.run(["system_profiler", "SPDisplaysDataType"], capture_output=True, text=True, timeout=30)
            names = [l.split(":", 1)[1].strip() for l in r.stdout.splitlines() if "Chipset Model" in l]
        elif shutil.which("lspci"):
            r = proc.run(["lspci"], capture_output=True, text=True, timeout=30)
            names = [l.split(": ", 1)[-1] for l in r.stdout.splitlines() if "VGA" in l or "3D controller" in l]
    except Exception:
        pass
    names = [n for n in names if "basic display" not in n.lower() and "remote" not in n.lower()]
    _gpu_names = names
    return names


def _nvidia_smi():
    exe = shutil.which("nvidia-smi")
    if exe or not sys.platform.startswith("win"):
        return exe
    import glob
    cands = [r"C:\Windows\System32\nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"]
    # newer (DCH) drivers sometimes keep it only in the driver store
    cands += sorted(glob.glob(r"C:\Windows\System32\DriverStore\FileRepository\nv*\nvidia-smi.exe"),
                    key=lambda p: -os.path.getmtime(p))
    return next((p for p in cands if os.path.exists(p)), None)


SMI_PROBLEM = [""]       # why nvidia-smi gave no answer, for the log


def nvidia_cuda():
    """CUDA version supported by the NVIDIA driver, e.g. (12, 8), or None."""
    exe = _nvidia_smi()
    if not exe:
        SMI_PROBLEM[0] = "nvidia-smi not found"
        return None
    try:
        r = proc.run([exe], capture_output=True, text=True, timeout=20)
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        SMI_PROBLEM[0] = f"{exe}: {e.__class__.__name__}: {e}"
        return None
    m = re.search(r"CUDA Version\s*:?\s*(\d+)\.(\d+)", out)
    if not m:
        SMI_PROBLEM[0] = f"{exe} answered without a CUDA version: {out.strip()[:300]!r}"
        return None
    SMI_PROBLEM[0] = ""
    return (int(m.group(1)), int(m.group(2)))


def nvidia_card():
    """Name of an NVIDIA card in this computer (from Windows / the system), even if nvidia-smi doesn't answer."""
    return next((n for n in gpu_names() if "nvidia" in n.lower()), None)


def has_nvidia():
    return bool(nvidia_cuda() or nvidia_card())


def plan():
    """What will be installed, in words, how big it is, and the torch index (None = default PyPI)."""
    p = _plan()
    big = bool(p["index"] and "cu1" in p["index"]) or (sys.platform.startswith("linux") and not p["index"])
    p["size"] = "about 3 GB" if big else "about 600 MB"
    p["runs_on"] = p["gpu"] or "your computer's processor (CPU)"
    return p


def _cuda_builds(card):
    """PyTorch CUDA builds to try, best guess first, from the card's generation (by name):
    - cu128: GTX 16 / RTX 20 and newer, including RTX 50 (Blackwell), which older builds can't run on
    - cu126: also older cards (GTX 900 / 10 series, Quadro M/P), which the newer builds dropped
    - cu130: the newest, as a last try."""
    n = (card or "").lower()
    old = re.search(r"gtx\s*(9\d\d|10\d\d)\b|titan x|quadro [mp]\d|tesla [mp]\d|\bgt\s*1030|mx\s*[1-3]\d\d", n)
    return ["cu126", "cu128", "cu130"] if old else ["cu128", "cu130", "cu126"]


def _plan():
    if sys.platform == "darwin":
        return {"gpu": "Apple GPU" if _apple_silicon() else None, "index": None}
    cuda = nvidia_cuda()
    card = nvidia_card()
    if cuda and cuda < (12, 0):
        return {"gpu": None, "index": TORCH_INDEX + "cpu",
                "note": "Your NVIDIA driver is too old for GPU separation; update it and reinstall to use the GPU."}
    if cuda or (card and not sys.platform == "darwin"):
        # (if Windows sees an NVIDIA card but nvidia-smi didn't answer, try anyway: the check after installing
        # tells whether it really works)
        builds = _cuda_builds(card)
        if cuda and cuda < (12, 8):
            builds = ["cu126"] + [b for b in builds if b != "cu126"]      # older drivers: the build they can run first
        return {"gpu": card or f"NVIDIA GPU (CUDA {cuda[0]}.{cuda[1]})", "index": TORCH_INDEX + builds[0],
                "builds": [TORCH_INDEX + b for b in builds]}
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
    for line in proc.lines(child, stall=1200, what="The download"):
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


def torch_device():
    """'cuda', 'mps', 'cpu', or None when PyTorch isn't installed (checked in a subprocess)."""
    try:
        r = proc.run([sys.executable, "-c", "import torch;print('cuda' if torch.cuda.is_available() else 'mps' if "
                      "getattr(torch.backends,'mps',None) and torch.backends.mps.is_available() else 'cpu')"],
                     capture_output=True, text=True, timeout=120)
        return r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def gpu_ready():
    """(can the graphics card be used, reason in words). For the Settings button and the setup check."""
    if sys.platform == "darwin":
        return (_apple_silicon(), "Apple Silicon Macs use their GPU automatically." if _apple_silicon()
                else "This Mac has no supported GPU; splitting runs on the processor.")
    cuda = nvidia_cuda()
    card = nvidia_card()
    if not cuda and card:
        return True, f"{card} found."
    if not cuda:
        names = gpu_names()
        found = ", ".join(names) if names else "no graphics card details"
        if any("nvidia" in n.lower() for n in names):
            return False, (f"This computer has {found}, but its NVIDIA driver isn't working (nvidia-smi didn't answer). "
                           f"Install the latest driver from nvidia.com (GeForce Game Ready or Studio), restart, "
                           f"then try again.")
        return False, (f"This computer has {found}. The separation model (Demucs on PyTorch) can only use NVIDIA "
                       f"graphics cards on Windows; AMD and Intel graphics aren't supported by it, so splitting runs "
                       f"on the processor. To speed it up: pick a faster model in step 2 (the ones marked fast), and "
                       f"leave Roformer off.")
    if not _plan()["gpu"]:
        return False, (f"Your NVIDIA driver supports CUDA {cuda[0]}.{cuda[1]}, which is too old for current PyTorch "
                       f"(needs 12.0 or newer). Update the driver from nvidia.com, then try again.")
    return True, f"{card or 'NVIDIA card'} found (driver supports CUDA {cuda[0]}.{cuda[1]})."


CHECK = ("import torch, demucs.pretrained, demucs.apply\n"
         "c=torch.cuda.is_available()\n"
         "if c: (torch.ones(8, device='cuda') * 2).sum().item()\n"
         "print('cuda' if c else 'mps' if getattr(torch.backends,'mps',None) and torch.backends.mps.is_available() else 'cpu')")
WRONG_BUILD = ("no kernel image", "not compatible with the current pytorch", "cuda capability", "too old",
               "sm_", "please check the instructions at https://pytorch.org")


def _remove_torch():
    proc.run([sys.executable, "-m", "pip", "uninstall", "-y", "torch", "torchaudio"], capture_output=True, text=True)
    forget_checks()


def install(progress=lambda f, m: None, gpu=False):
    """Install everything. progress(fraction or None, message).
    On an NVIDIA card: installs the CUDA build that fits the card's generation, tests a real calculation on the
    card, and if the card rejects that build ("no kernel image"), removes it and tries the next build.
    pip won't swap builds of the same version by itself (it calls the old one "already satisfied"), so the old
    one is removed first whenever it isn't already working on the card."""
    p = plan()
    where = p["gpu"] or "your CPU"
    progress(0.01, f"Setting up for {where}")
    if gpu and not p["gpu"]:
        raise RuntimeError(gpu_ready()[1])
    builds = p.get("builds") or [p["index"]]
    if p["gpu"] and torch_device() == "cuda":
        builds = [None]                         # already on the card: just make sure Demucs is there
    tried = []
    for bi, index in enumerate(builds):
        lo = 0.02 + 0.78 * bi / len(builds)
        hi = 0.02 + 0.78 * (bi + 1) / len(builds)
        if index is not None:
            if p["gpu"]:
                progress(lo, "Removing the PyTorch version that doesn't run on your card" if bi else
                         "Removing the processor-only PyTorch so the graphics-card version can go in")
                if bi or torch_device() is not None:
                    _remove_torch()
            tag = index.rsplit("/", 1)[-1]
            args = ["torch", "torchaudio", "--index-url", index] + (["--no-cache-dir"] if p["gpu"] else [])
            _pip(args, progress, lo, hi, "Installing PyTorch" + (f" for your graphics card ({tag}, about 3 GB)" if p["gpu"] else ""))
        if bi == 0 or index is not None:
            _pip(DEMUCS_DEPS, progress, 0.80, 0.92, "Installing helpers")
            _pip(["--no-deps", "demucs"], progress, 0.92, 0.97, "Installing Demucs")
        progress(0.98, "Testing it on your graphics card" if p["gpu"] else "Checking the installation")
        r = proc.run([sys.executable, "-c", CHECK], capture_output=True, text=True)
        err = (r.stderr or "")
        if r.returncode == 0:
            break
        if p["gpu"] and any(w in err.lower() for w in WRONG_BUILD) and bi + 1 < len(builds):
            tried.append(index.rsplit("/", 1)[-1] if index else "current")
            progress(None, f"Your card needs a different PyTorch build; trying {builds[bi + 1].rsplit('/', 1)[-1]}")
            continue
        if p["gpu"] and "torch" in err:
            raise RuntimeError("PyTorch installed, but it couldn't run on your graphics card"
                               + (f" (tried {', '.join(tried + [index.rsplit('/', 1)[-1]])})" if index else "") + ":\n"
                               + err[-500:] + "\nUpdate the NVIDIA driver from nvidia.com (newer cards need a recent "
                               "one), restart the computer, then use Use my graphics card again.")
        raise RuntimeError("Installed, but it doesn't load:\n" + err[-600:])
    importlib.invalidate_caches()
    dev = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "cpu"
    forget_checks()
    if p["gpu"] and dev == "cpu" and sys.platform != "darwin":
        raise RuntimeError("PyTorch installed, but it still can't see your NVIDIA card. Restart the computer (a new "
                           "driver needs it), update the driver from nvidia.com, then use Use my graphics card again.")
    progress(1.0, "Separation model installed" + (" (runs on your graphics card)" if dev == "cuda" else ""))
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


def extra_installed(key, fresh=False):
    return _has(EXTRAS[key]["module"], fresh)


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
    forget_checks()
    progress(1.0, f"{e['label']} installed")
