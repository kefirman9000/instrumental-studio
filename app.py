#!/usr/bin/env python3
"""Instrumental Studio — launch the app in its own window.

    python app.py              open the app window
    python app.py --browser    use your normal browser instead of a window
    python app.py --port 8765  fixed port (default: any free port)
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from studio import diag  # noqa: E402
diag.setup()             # the log, crash and freeze recording start before anything else loads
diag.mark("loading the app")
from studio.server import serve, HOME  # noqa: E402
diag.mark("app loaded")

TITLE = "Instrumental Studio"


class JsApi:
    """Native file pickers exposed to the page when running inside pywebview."""

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
        """Works even if the app's server is stuck, because it runs in the window's own process."""
        from studio import updater, proc
        proc.kill_all()
        updater.relaunch()
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


def main():
    ap = argparse.ArgumentParser(description=TITLE)
    ap.add_argument("--browser", action="store_true", help="open in your default browser instead of a window")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--after-update", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--safe", action="store_true", help="start without update checks or hardware probes")
    args = ap.parse_args()

    # messages always go to app.log too (set up by diag at the top of this file)

    from studio.updater import RESTART
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
    diag.mark("starting the server" + (" (safe mode)" if safe else ""))
    httpd, url = serve(args.port, just_updated=args.after_update, safe=safe)
    diag.mark(f"server ready at {url}")
    print(f"{TITLE} running at {url}  (projects in {HOME})")

    def on_restart(close):
        RESTART.wait()
        time.sleep(0.5)
        try:
            close()
        finally:
            httpd.shutdown()
            os._exit(0)

    if not args.browser:
        try:
            import webview  # pip install pywebview
            api = JsApi()
            win = webview.create_window(TITLE, url, width=1480, height=920, min_size=(1100, 700), js_api=api,
                                        background_color="#111318")
            api.window = win
            diag.mark("opening the window (pywebview " + getattr(webview, "__version__", "?") + ")")
            threading.Thread(target=on_restart, args=(win.destroy,), daemon=True).start()
            webview.start()
            httpd.shutdown()
            return
        except ImportError:
            pass
        diag.mark("pywebview not installed: using a browser window")
        browser = find_chromium()
        if browser:
            profile = os.path.join(tempfile.gettempdir(), "instrumental-studio-window")
            proc = subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}",
                                     "--window-size=1480,920", "--autoplay-policy=no-user-gesture-required"])
            threading.Thread(target=on_restart, args=(proc.terminate,), daemon=True).start()
            try:
                proc.wait()   # dedicated profile, so this returns when the app window is closed
            except KeyboardInterrupt:
                pass
            if not RESTART.is_set():
                httpd.shutdown()
            else:
                time.sleep(2)
            return
    webbrowser.open(url)
    threading.Thread(target=on_restart, args=(lambda: None,), daemon=True).start()
    print("Press Ctrl+C to quit.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
