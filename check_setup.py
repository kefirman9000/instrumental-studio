#!/usr/bin/env python3
"""Check everything Instrumental Studio needs, and install what's missing.

    python check_setup.py          check, then offer to fix each problem (asks first)
    python check_setup.py --yes    fix everything without asking (except the big model download)
    python check_setup.py --check  only report, change nothing
    python check_setup.py --gpu    switch splitting to your NVIDIA graphics card

Uses only Python's own library until the app's packages are confirmed, so it works on a fresh install.
Also writes what it found to app.log.
"""
import importlib.util
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ARGS = set(sys.argv[1:])
ONLY_CHECK = "--check" in ARGS
YES = "--yes" in ARGS
FROM_APP = "--from-app" in ARGS
GPU = "--gpu" in ARGS          # switch splitting to the NVIDIA graphics card without asking
WIN = sys.platform.startswith("win")
MAC = sys.platform == "darwin"

OK, WARN, BAD = "  OK  ", " NOTE ", " FIX  "
problems = []


def say(tag, what, detail=""):
    print(f"[{tag}] {what}" + (f"\n         {detail}" if detail else ""))


def ask(q, default=True):
    if ONLY_CHECK:
        return False
    if YES:
        return True
    try:
        a = input(f"         {q} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except EOFError:
        return default
    return default if not a else a in ("y", "yes")


def has(mod):
    try:
        importlib.invalidate_caches()
    except Exception:
        pass
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def pip(*args):
    cmd = [sys.executable, "-m", "pip", "install", *args]
    print("         running: " + " ".join(cmd))
    return subprocess.call(cmd) == 0


def check_python():
    v = sys.version_info
    where = sys.executable
    if v < (3, 10):
        say(BAD, f"Python {v.major}.{v.minor} is too old", f"Install Python 3.12 or 3.13 from python.org ({where}).")
        problems.append("python")
    elif v >= (3, 14):
        say(WARN, f"Python {v.major}.{v.minor}", "Very new: the separation model may not support it yet. "
                                                   "If its install fails, install Python 3.12 or 3.13 from python.org.")
    elif v.releaselevel != "final":
        say(WARN, f"Python {sys.version.split()[0]} is a pre-release (test) version",
            f"Pre-releases can have bugs. Install the final Python {v.major}.{v.minor} from python.org ({where}).")
    else:
        say(OK, f"Python {v.major}.{v.minor}.{v.micro}", where)


def check_pip():
    if subprocess.call([sys.executable, "-m", "pip", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        say(OK, "pip (installs Python packages)")
        return True
    say(BAD, "pip is missing")
    if ask("Set up pip now?"):
        if subprocess.call([sys.executable, "-m", "ensurepip", "--upgrade"]) == 0:
            say(OK, "pip installed")
            return True
    problems.append("pip")
    return False


def check_packages(pip_ok):
    need = [("numpy", "numpy", True), ("scipy", "scipy", True), ("webview", "pywebview", False)]
    missing = [(m, p, req) for m, p, req in need if not has(m)]
    for m, p, req in need:
        if (m, p, req) not in missing:
            say(OK, f"{p}")
    if not missing:
        return
    for m, p, req in missing:
        say(BAD if req else WARN, f"{p} is missing",
            "Needed to run the app." if req else "Gives the app its own window; without it the app opens in Chrome or Edge.")
    if pip_ok and ask("Install the missing packages now?"):
        if pip("-r", os.path.join(HERE, "requirements.txt")):
            still = [p for m, p, req in missing if not has(m)]
            if not still:
                say(OK, "Packages installed")
                return
            say(BAD, "Still missing: " + ", ".join(still))
        else:
            say(BAD, "Installing the packages failed (see the messages above).")
    if any(req for m, p, req in missing if not has(m)):
        problems.append("packages")


def check_ffmpeg():
    if shutil.which("ffmpeg"):
        say(OK, "ffmpeg (reads MP3, M4A and other formats)")
        return
    say(WARN, "ffmpeg is missing", "Without it only WAV songs open. MP3, M4A, FLAC and the rest need it.")
    if WIN and shutil.which("winget"):
        if ask("Install ffmpeg with winget now?"):
            subprocess.call(["winget", "install", "--id", "Gyan.FFmpeg", "-e", "--accept-source-agreements",
                             "--accept-package-agreements"])
            say(WARN, "If it installed, close this window and start the app again so it can find ffmpeg.")
            return
    elif MAC and shutil.which("brew"):
        if ask("Install ffmpeg with Homebrew now?"):
            subprocess.call(["brew", "install", "ffmpeg"])
            return
    cmd = ("winget install ffmpeg" if WIN else "brew install ffmpeg  (needs https://brew.sh)" if MAC
           else "sudo apt install ffmpeg")
    say(WARN, f"To install it yourself, run: {cmd}")


def check_webview2():
    if not WIN or not has("webview"):
        return
    try:
        import winreg
        keys = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")]
        for root, k in keys:
            try:
                with winreg.OpenKey(root, k) as h:
                    v, _ = winreg.QueryValueEx(h, "pv")
                    if v and v != "0.0.0.0":
                        say(OK, f"Microsoft Edge WebView2 {v} (draws the app window)")
                        return
            except OSError:
                continue
    except Exception:
        return
    say(WARN, "Microsoft Edge WebView2 wasn't found",
        "The app window needs it. Get the Evergreen Runtime from "
        "https://developer.microsoft.com/microsoft-edge/webview2/ (Windows 11 already has it).")


def check_model():
    try:
        from studio import installer as INST
    except Exception as e:
        say(WARN, f"Couldn't check the separation model: {e}")
        return
    if INST.installed(fresh=True):
        say(OK, "Separation model (Demucs + PyTorch)")
        try:
            cuda = INST.has_nvidia()
            r = subprocess.run([sys.executable, "-c", "import torch;print(torch.cuda.is_available())"],
                               capture_output=True, text=True, timeout=90)
            on_gpu = r.stdout.strip().endswith("True")
            if cuda and not on_gpu:
                ok, why = INST.gpu_ready()
                say(WARN, "Your NVIDIA graphics card isn't used for splitting",
                    "Splitting could be 5 to 10 times faster." if ok else why)
                if ok and not ONLY_CHECK and (GPU or (not YES and ask(
                        "Switch to the graphics-card version now (downloads about 3 GB)?", default=True))):
                    def prog(f, m):
                        print(f"         {int(round((f or 0) * 100)):3d}%  {m}" if f is not None else f"         {m}")
                    try:
                        INST.install(prog, gpu=True)
                        say(OK, "Splitting now uses your NVIDIA graphics card")
                    except Exception as e:
                        say(BAD, f"Couldn't switch to the graphics card: {e}")
            elif on_gpu:
                say(OK, "Splitting uses your NVIDIA graphics card")
        except Exception:
            pass
    else:
        try:
            p = INST.plan()
            size, where = p["size"], p["runs_on"]
        except Exception:
            size, where = "about 1-3 GB", "this computer"
        say(WARN, "Separation model isn't installed",
            f"Needed only for splitting songs: {size}, runs on {where}. Opening songs, importing stems and "
            f"the demo work without it. You can also install it from the app's start screen.")
        if not ONLY_CHECK and (not YES) and ask(f"Download and install it now ({size})?", default=False):
            def prog(f, m):
                print(f"         {int(round((f or 0) * 100)):3d}%  {m}" if f is not None else f"         {m}")
            try:
                INST.install(prog)
                say(OK, "Separation model installed")
            except Exception as e:
                say(BAD, f"The model didn't install: {e}")
    for key, e in getattr(INST, "EXTRAS", {}).items():
        try:
            state = "installed" if INST.extra_installed(key, fresh=True) else "not installed (optional, from the start screen)"
        except Exception:
            state = "unknown"
        say(OK if "not" not in state else WARN, f"{e.get('label', key)}: {state}")


def check_disk():
    home = os.environ.get("INSTRUMENTAL_STUDIO_HOME", os.path.join(os.path.expanduser("~"), "InstrumentalStudio"))
    try:
        os.makedirs(home, exist_ok=True)
        free = shutil.disk_usage(home).free / 1e9
    except OSError as e:
        say(BAD, f"Can't use the projects folder {home}: {e}")
        problems.append("folder")
        return
    if free < 5:
        say(WARN, f"Only {free:.1f} GB free", "Songs, splits and the model need several GB. Free some space.")
    else:
        say(OK, f"{free:.0f} GB free for songs and splits ({home})")


def main():
    print(f"Instrumental Studio setup check  |  {platform.platform()}\n")
    if FROM_APP:
        print("The app couldn't start because something it needs is missing. Let's fix that.\n")
    check_python()
    pip_ok = check_pip()
    check_packages(pip_ok)
    check_ffmpeg()
    check_webview2()
    check_disk()
    check_model()
    print()
    if problems:
        print("Something still needs fixing (marked FIX above). The app won't start until it's sorted.")
    else:
        print("Everything the app needs is in place." + (" Start it again." if FROM_APP else ""))
    try:
        from studio import diag
        diag.append(f"setup check: {'problems: ' + ', '.join(problems) if problems else 'all good'}")
    except Exception:
        pass
    return 1 if problems else 0


if __name__ == "__main__":
    code = main()
    if FROM_APP:
        try:
            input("\nPress Enter to close this window.")
        except EOFError:
            pass
    sys.exit(code)
