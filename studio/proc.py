"""Run helper programs (ffmpeg, pip, the separation worker, nvidia-smi, git) without flashing a
console window on Windows. Elsewhere this changes nothing."""
import subprocess
import sys

WINDOWS = sys.platform.startswith("win")


def hidden():
    if not WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}


def run(cmd, **kw):
    return subprocess.run(cmd, **{**hidden(), **kw})


def popen(cmd, **kw):
    return subprocess.Popen(cmd, **{**hidden(), **kw})


def gui_python():
    """pythonw.exe next to python.exe on Windows (no console), else the current interpreter."""
    import os
    exe = sys.executable
    if WINDOWS and os.path.basename(exe).lower() == "python.exe":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe
