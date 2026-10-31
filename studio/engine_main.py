"""The engine: the app's server and all audio work, in its own process.

app.py (the window) starts this. Keeping the heavy work here means nothing it does (decoding a song,
loading the analysis libraries, splitting, repairing) can freeze the window: Python runs one thing at a
time per process, and the window's process now does nothing but draw the window.

    python -m studio.engine_main --port N [--after-update] [--safe] [--browser]

Exits when the window process goes away (--parent PID is watched), or when a restart is asked for.
"""
import argparse
import os
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

from studio import diag  # noqa: E402


def _watch_parent(pid):
    """When the window process ends (closed, or crashed), stop too, so no invisible engine is left
    running. Windows: wait on the process itself. Elsewhere: notice when we're handed to a new parent."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x00100000, False, int(pid))          # SYNCHRONIZE
            if not h:
                diag.line(f"engine: can't watch the window process {pid}; carrying on without")
                return
            k32.WaitForSingleObject(h, 0xFFFFFFFF)                     # until it ends
        else:
            while os.getppid() == int(pid):
                time.sleep(1.0)
    except Exception as e:
        diag.line(f"engine: watching the window process failed ({e!r}); carrying on without")
        return
    diag.line("engine: the window process is gone, stopping")
    os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--after-update", action="store_true")
    ap.add_argument("--safe", action="store_true")
    ap.add_argument("--browser", action="store_true")
    ap.add_argument("--parent", type=int, default=0)
    a = ap.parse_args()
    diag.setup(tag="engine")
    # a restart from here (after an update, or Troubleshooting) starts app.py again with these
    sys.argv = [os.path.join(APP_DIR, "app.py")] + (["--browser"] if a.browser else [])
    if a.parent:
        threading.Thread(target=_watch_parent, args=(a.parent,), name="watch-window", daemon=True).start()
    diag.mark("engine: loading")
    try:
        from studio.server import serve
        from studio.updater import RESTART
    except Exception as e:
        diag.line(f"engine: can't load the app: {e!r}")
        sys.exit(3)                # tells the window process to open the setup check
    standalone = not os.environ.get("STUDIO_TOKEN")
    httpd, url = serve(a.port, just_updated=a.after_update, safe=a.safe)
    diag.mark(f"engine: ready at {url}")
    if standalone:                   # started by hand: the address with this session's secret, to open yourself
        from studio.server import TOKEN
        print(f"Open {url}#t={TOKEN}", flush=True)
    RESTART.wait()
    diag.line("engine: restarting")
    time.sleep(0.5)
    try:
        httpd.shutdown()
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
