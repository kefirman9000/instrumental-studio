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
SPONSORS_URL = "https://github.com/sponsors/kefirman9000"
HOME = diag.home_dir()


def _open_setup_check(why, then_exit=True):
    """Something the app needs is missing or broken: open the setup checker in its own window (it
    explains and offers to install), instead of failing silently with no console."""
    diag.line(f"can't start: {why}. Opening the setup check.")
    _launch_setup_check()
    if then_exit:
        sys.exit(1)


def _launch_setup_check():
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


class JsApi:
    """Native file pickers exposed to the page when running inside pywebview. These run in this (window)
    process, which is never busy, so they always respond."""

    def __init__(self):
        # Leading underscore: pywebview exposes every public attribute of this object to the page and
        # walks into it. A public `window` made it crawl through the whole native window at load
        # (the long "Error while processing window.native..." lines in app.log).
        self._window = None

    def _dialog(self, kind, **kw):
        import webview
        fd = getattr(webview, "FileDialog", None)
        const = {"open": getattr(fd, "OPEN", None) if fd else getattr(webview, "OPEN_DIALOG", 10),
                 "folder": getattr(fd, "FOLDER", None) if fd else getattr(webview, "FOLDER_DIALOG", 20)}[kind]
        res = self._window.create_file_dialog(const, **kw)
        if not res:
            return None
        return res[0] if isinstance(res, (list, tuple)) else res

    def choose_song(self):
        return self._dialog("open", allow_multiple=False,
                            file_types=("Audio files (*.wav;*.mp3;*.flac;*.m4a;*.aac;*.ogg;*.aiff;*.aif;*.opus)",
                                        "All files (*.*)"))

    def open_sponsors(self):
        """Open the GitHub Sponsors page in the normal browser. Only this one address can be opened."""
        webbrowser.open(SPONSORS_URL)
        return True

    def choose_folder(self):
        return self._dialog("folder")

    def setup_check(self):
        _launch_setup_check()
        return True

    def export_log(self):
        """Works without the engine: the log lives in a file this process can read too."""
        path = diag.export_bundle(os.path.join(HOME, "logs"), {"Exported from": "the start-up error page"})
        _show_in_folder(path)
        return path

    def open_log(self):
        _show_in_folder(diag.log_path())
        return True

    def restart(self):
        """Works even if the engine is stuck: it runs here, stops the engine and starts the app again."""
        from studio import updater, proc
        updater.relaunch()       # start the new copy first: stopping the engine closes this window
        proc.kill_all()
        return True


def _show_in_folder(path):
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception as e:
        diag.line(f"couldn't show {path}: {e}")


STARTING_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{background:#111318;color:#e6e9ef;font:15px system-ui,-apple-system,Segoe UI,sans-serif;margin:0;height:100vh;
display:grid;place-items:center}.c{text-align:center}.sp{width:28px;height:28px;border-radius:50%;border:3px solid #2a2f3a;
border-top-color:#e0a050;animation:s .9s linear infinite;margin:0 auto 16px}@keyframes s{to{transform:rotate(360deg)}}
p{color:#8a93a6}</style></head><body><div class="c"><div class="sp"></div><h2>Starting Instrumental Studio</h2>
<p>Starting the engine. The first start after an update can take a little longer.</p></div></body></html>"""


def error_html(reason):
    import html
    try:
        with open(diag.log_path(), encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-40:])
    except OSError:
        tail = "(no log yet)"
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{background:#111318;color:#e6e9ef;font:15px system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:28px 34px}}
h2{{margin:0 0 8px}}p{{color:#a7afbf;line-height:1.5;max-width:760px}}
button{{background:#232836;color:#e6e9ef;border:1px solid #39404f;border-radius:7px;padding:8px 14px;margin:4px 8px 4px 0;
font:inherit;cursor:pointer}}button.p{{background:#e0a050;color:#111318;border-color:#e0a050;font-weight:600}}
pre{{background:#0b0d12;border:1px solid #2a2f3a;border-radius:7px;padding:12px;max-height:46vh;overflow:auto;
font-size:12px;color:#c6ccd8;white-space:pre-wrap}}</style></head><body>
<h2>Instrumental Studio couldn't finish starting</h2>
<p>{html.escape(reason)}</p>
<p><button class="p" onclick="pywebview.api.export_log()">Export log</button>
<button onclick="pywebview.api.setup_check()">Check and fix setup</button>
<button onclick="pywebview.api.open_log()">Show the log file</button>
<button onclick="pywebview.api.restart()">Try again</button></p>
<p>Export log saves one file to attach when asking for help. The end of the log:</p>
<pre>{html.escape(tail)}</pre></body></html>"""


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
    cmd = [proc.gui_python(), "-m", "studio.engine_main", "--port", str(port), "--parent", str(os.getpid())]
    cmd += ["--after-update"] if args.after_update else []
    cmd += ["--safe"] if safe else []
    cmd += ["--browser"] if args.browser else []
    # The engine watches this process and stops when it ends. proc.popen tracks it, so Restart
    # (proc.kill_all) stops it as well. Its output goes to app.log, never to a pipe nobody reads.
    return proc.popen(cmd, cwd=APP_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)


def wait_for_engine(url, engine, timeout=60):
    """"ready" once the engine answers, "exited" if it stopped first, "timeout" if it never answered.
    Ignores any system proxy settings."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    while time.time() - t0 < timeout:
        if engine.poll() is not None:
            return "exited"
        try:
            with opener.open(url, timeout=2) as r:
                if r.status == 200:
                    return "ready"
        except Exception:
            pass
        time.sleep(0.1)
    return "timeout"


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
    holder = {"closing": False}

    def launch():
        diag.mark("starting the engine" + (" (safe mode)" if safe else ""))
        holder["engine"] = start_engine(port, args, safe)
        st = wait_for_engine(url, holder["engine"])
        if st == "ready":
            diag.mark(f"engine answering at {url}")
            print(f"{TITLE} running at {url}  (projects in {HOME})")
            return None
        code = holder["engine"].poll()
        why = (f"The engine stopped while starting (exit code {code})." if st == "exited"
               else "The engine didn't answer within a minute.")
        diag.line(f"start-up failed: {why}")
        return why + " Something it needs may be missing: try Check and fix setup. Export log saves the details."

    def stop_engine():
        holder["closing"] = True
        e = holder.get("engine")
        if e is not None:
            proc.kill(e)

    def follow(close):
        """Close the window when the engine ends (a restart from inside the app, or a crash), or when a
        restart is asked for from this window."""
        def on_engine_exit():
            code = holder["engine"].wait()
            if holder["closing"]:
                diag.line("engine stopped (the window was closed)")
            elif not RESTART.is_set():
                diag.line(f"the engine ended unexpectedly (exit code {code}); closing the window")
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
            # the window opens straight away with a "Starting" page; the app loads into it once the
            # engine answers, or an error page with the log and what to do shows instead
            win = webview.create_window(TITLE, html=STARTING_HTML, width=1480, height=920, min_size=(1100, 700),
                                        js_api=api, background_color="#111318")
            api._window = win
            try:
                from importlib.metadata import version as _v
                wv = _v("pywebview")
            except Exception:
                wv = getattr(webview, "__version__", "?")
            diag.mark(f"opening the window (pywebview {wv})")

            def boot():
                problem = launch()
                if problem:
                    win.load_html(error_html(problem))
                    return
                follow(win.destroy)
                win.load_url(url)
            webview.start(boot)
            diag.line("window closed")
            stop_engine()
            return
        problem = launch()
        if problem:
            _open_setup_check(problem)
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
    if "engine" not in holder:             # --browser: nothing above started it
        problem = launch()
        if problem:
            _open_setup_check(problem)
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
