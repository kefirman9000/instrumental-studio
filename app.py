#!/usr/bin/env python3
"""Instrumental Studio — launch the app in its own window.

    python app.py              open the app window
    python app.py --browser    use your normal browser instead of a window
    python app.py --port 8765  fixed port (default: any free port)

Two processes: this one only draws the window (and opens file pickers); the engine (studio/engine_main.py)
does everything else. Python runs one thing at a time per process, so with the heavy work elsewhere,
opening or processing a song can never freeze the window.
"""
import argparse
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
from studio import diag  # noqa: E402
diag.setup(tag="window")   # the log, crash and freeze recording start before anything else loads
diag.mark("starting")

TITLE = "Instrumental Studio"
HOME = diag.home_dir()


def _open_setup_check(why):
    """Something the app needs is missing or broken: open the setup checker in its own window (it
    explains and offers to install), instead of failing silently with no console."""
    diag.line(f"can't start: {why}. Opening the setup check.")
    script = os.path.join(APP_DIR, "check_setup.py")
    try:
        if sys.platform.startswith("win"):
            exe = sys.executable
            if os.path.basename(exe).lower() == "pythonw.exe":
                exe = os.path.join(os.path.dirname(exe), "python.exe")
            subprocess.Popen([exe, script, "--from-app"], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                             cwd=APP_DIR)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Terminal", os.path.join(APP_DIR, "Check and fix setup (Mac).command")])
        else:
            print(f"Instrumental Studio can't start: {why}\nRun: python3 {script}", file=sys.stderr)
    except Exception as e:
        diag.line(f"couldn't open the setup check: {e}")
    sys.exit(1)


class JsApi:
    """Native file pickers exposed to the page when running inside pywebview. These run in this (window)
    process, which is never busy, so they always respond."""

    def __init__(self):
        self.window = None

    def _dialog(self, kind, **kw):
        import webview
        fd = getattr(webview, "FileDialog", None)
        const = {"open": getattr(fd, "OPEN", None) if fd else getattr(webview, "OPEN_DIALOG", 10),
                 "folder": getattr(fd, "FOLDER", None) if fd else getattr(webview, "FOLDER_DIALOG", 20)}[kind]
        res = self.window.create_file_dialog(const, **kw)
        if not res:
            return None
        return res[0] if isinstance(res, (list, tuple)) else res

    def choose_song(self):
        return self._dialog("open", allow_multiple=False,
                            file_types=("Audio files (*.wav;*.mp3;*.flac;*.m4a;*.aac;*.ogg;*.aiff;*.aif;*.opus)",
                                        "All files (*.*)"))

    def choose_folder(self):
        return self._dialog("folder")

    def restart(self):
        """Works even if the engine is stuck: it runs here, stops the engine and starts the app again."""
        from studio import updater, proc
        updater.relaunch()       # start the new copy first: stopping the engine closes this window
        proc.kill_all()
        return True


def find_chromium():
    cands = []
    if sys.platform.startswith("win"):
        for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    elif sys.platform == "darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "msedge", "chrome"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    return next((c for c in cands if c and os.path.exists(c)), None)


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def start_engine(port, args, safe):
    from studio import proc
    cmd = [proc.gui_python(), "-m", "studio.engine_main", "--port", str(port), "--watch-parent"]
    cmd += ["--after-update"] if args.after_update else []
    cmd += ["--safe"] if safe else []
    cmd += ["--browser"] if args.browser else []
    # stdin stays open as a lifeline: when this process ends, the engine sees it close and stops too.
    # proc.popen tracks it, so Restart (proc.kill_all) stops it as well.
    return proc.popen(cmd, cwd=APP_DIR, stdin=subprocess.PIPE)


def wait_for_engine(url, engine, timeout=90):
    """True once the engine answers; False if it exited first. Ignores any system proxy settings."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    while time.time() - t0 < timeout:
        if engine.poll() is not None:
            return False
        try:
            with opener.open(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.1)
    return True        # still starting: open the window anyway; it shows that it's waiting


def main():
    ap = argparse.ArgumentParser(description=TITLE)
    ap.add_argument("--browser", action="store_true", help="open in your default browser instead of a window")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--after-update", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--safe", action="store_true", help="start without update checks or hardware probes")
    args = ap.parse_args()

    # Quick look (nothing is loaded) for the parts the engine can't run without
    for mod in ("numpy", "scipy"):
        if importlib.util.find_spec(mod) is None:
            _open_setup_check(f"{mod} isn't installed")

    from studio.updater import RESTART
    from studio import proc
    # If the last start never reached the point where the window showed up, start in safe mode:
    # no update check, no graphics-card probe. The window removes the marker once it has loaded.
    marker = os.path.join(HOME, ".starting")
    safe = args.safe or os.path.exists(marker)
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(marker, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass

    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}/"
    diag.mark("starting the engine" + (" (safe mode)" if safe else ""))
    engine = start_engine(port, args, safe)
    if not wait_for_engine(url, engine):
        code = engine.poll()
        _open_setup_check(f"the engine stopped while starting (exit code {code}); details are in app.log")
    diag.mark(f"engine answering at {url}")
    print(f"{TITLE} running at {url}  (projects in {HOME})")

    def stop_engine():
        try:
            engine.stdin.close()
        except Exception:
            pass
        proc.kill(engine)

    def follow(close):
        """Close the window when the engine ends (a restart from inside the app, or a crash), or when a
        restart is asked for from this window."""
        def on_engine_exit():
            code = engine.wait()
            if not RESTART.is_set():
                diag.line(f"the engine ended (exit code {code}); closing the window")
            try:
                close()
            finally:
                time.sleep(0.3)
                os._exit(0)

        def on_restart():
            RESTART.wait()
            time.sleep(0.5)
            try:
                close()
            finally:
                stop_engine()
                os._exit(0)
        threading.Thread(target=on_engine_exit, daemon=True).start()
        threading.Thread(target=on_restart, daemon=True).start()

    if not args.browser:
        try:
            import webview  # pip install pywebview
        except ImportError:
            webview = None
        if webview is not None:
            api = JsApi()
            win = webview.create_window(TITLE, url, width=1480, height=920, min_size=(1100, 700), js_api=api,
                                        background_color="#111318")
            api.window = win
            diag.mark("opening the window (pywebview " + getattr(webview, "__version__", "?") + ")")
            follow(win.destroy)
            webview.start()
            diag.line("window closed")
            stop_engine()
            return
        diag.mark("pywebview not installed: using a browser window")
        browser = find_chromium()
        if browser:
            profile = os.path.join(tempfile.gettempdir(), "instrumental-studio-window")
            bproc = subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}",
                                      "--window-size=1480,920", "--autoplay-policy=no-user-gesture-required"])
            follow(bproc.terminate)
            try:
                bproc.wait()   # dedicated profile, so this returns when the app window is closed
            except KeyboardInterrupt:
                pass
            if not RESTART.is_set():
                stop_engine()
            else:
                time.sleep(2)
            return
    webbrowser.open(url)
    follow(lambda: None)
    print("Press Ctrl+C to quit.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_engine()


if __name__ == "__main__":
    main()
