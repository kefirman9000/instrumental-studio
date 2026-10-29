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


import queue
import threading
import time

CHILDREN = set()            # helper processes that are running right now
_lock = threading.Lock()
CANCEL = threading.Event()  # set when the person presses Stop


class Stopped(RuntimeError):
    pass


def popen(cmd, **kw):
    extra = {} if WINDOWS else {"start_new_session": True}     # so the whole tree can be stopped
    p = subprocess.Popen(cmd, **{**hidden(), **extra, **kw})
    with _lock:
        CHILDREN.add(p)
    return p


def kill(p):
    """Stop a helper and anything it started."""
    try:
        if p.poll() is not None:
            return
        if WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, **hidden())
        else:
            import os
            import signal
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    finally:
        try:
            p.wait(timeout=5)
        except Exception:
            pass
        with _lock:
            CHILDREN.discard(p)


def kill_all():
    with _lock:
        ps = list(CHILDREN)
    for p in ps:
        kill(p)
    return len(ps)


def lines(p, stall=900, what="The separation"):
    """Yield a helper's output lines without ever hanging: stops it if the person presses Stop, or if
    it prints nothing for `stall` seconds (stuck download, frozen GPU driver...)."""
    q = queue.Queue()

    def reader():
        try:
            for line in iter(p.stdout.readline, ""):
                q.put(line)
        finally:
            q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    last = time.time()
    try:
        while True:
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                if CANCEL.is_set():
                    kill(p)
                    raise Stopped("Stopped.")
                if time.time() - last > stall:
                    kill(p)
                    span = f"{stall // 60} minutes" if stall >= 120 else f"{stall} seconds"
                    raise RuntimeError(f"{what} showed no progress for {span}, so it was stopped. "
                                       "Check your internet connection (models download the first time), or use "
                                       "Settings, Troubleshooting, to clear the downloaded model files and try again.")
                continue
            if item is None:
                break
            last = time.time()
            yield item
        p.wait()
    finally:
        with _lock:
            CHILDREN.discard(p)
        if CANCEL.is_set():
            raise Stopped("Stopped.")


def gui_python():
    """pythonw.exe next to python.exe on Windows (no console), else the current interpreter."""
    import os
    exe = sys.executable
    if WINDOWS and os.path.basename(exe).lower() == "python.exe":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe
