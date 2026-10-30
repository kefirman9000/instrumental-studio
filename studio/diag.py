"""Diagnostics: an always-on log, a freeze detector, and an exportable bundle for support.

- Everything the app prints goes to InstrumentalStudio/app.log as well as the console (if any).
  The log is rotated at 5 MB (one older copy is kept as app.old.log).
- Crashes are written down: Python errors in any thread, and hard crashes (faulthandler).
- Freezes are written down: a watchdog that runs outside Python's lock dumps what every thread was
  doing whenever the app stays blocked for more than FREEZE_S seconds. That is the one thing that
  says exactly what froze the window.
- export_bundle() zips the logs with a short description of this computer and install.
"""
import datetime
import faulthandler
import os
import platform
import sys
import threading
import time
import traceback
import zipfile

FREEZE_S = 3.0
START = time.time()
_state = {"file": None, "home": None, "tag": ""}


def home_dir():
    return os.environ.get("INSTRUMENTAL_STUDIO_HOME", os.path.join(os.path.expanduser("~"), "InstrumentalStudio"))


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log_dir(home=None):
    """The logs folder next to the app's start files (falls back to the projects folder if the app
    folder can't be written to)."""
    d = os.path.join(APP_DIR, "logs")
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return d
    except OSError:
        d = home or _state["home"] or home_dir()
        os.makedirs(d, exist_ok=True)
        return d


def log_path(home=None):
    return os.path.join(log_dir(home), "app.log")


def errors_path(home=None):
    return os.path.join(log_dir(home), "errors.log")


ERROR_WORDS = ("Traceback", "UNCAUGHT", "FREEZE", "failed", "skipped", "Error", "error:", "refused", "can't", "couldn't")


def _to_errors(text):
    try:
        with open(errors_path(), "a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


class _Tee:
    """Writes to the log file and, when there is one, the console."""

    def __init__(self, console, f):
        self.console, self.f = console, f

    def write(self, s):
        try:
            self.f.write(s)
        except Exception:
            pass
        # Python error reports ("Traceback ...") also go, whole, to errors.log
        try:
            if "Traceback (most recent call last)" in s:
                self.tb = ""
            if getattr(self, "tb", None) is not None:
                self.tb += s
                last = [l for l in self.tb.splitlines() if l.strip()]
                if last and not last[-1].startswith((" ", "\t", "Traceback")) and len(last) > 1:
                    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _to_errors(f"[{stamp}]\n{self.tb}")
                    self.tb = None
        except Exception:
            self.tb = None
        if self.console is not None:
            try:
                self.console.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        for x in (self.f, self.console):
            try:
                if x is not None:
                    x.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def fileno(self):
        return self.f.fileno()


def setup(home=None, tag=None):
    """Call first thing at startup. Safe to call more than once. tag labels this process's lines
    ("window" or "engine"), since both write to the same log."""
    if tag:
        _state["tag"] = tag
    if _state["file"] is not None:
        return _state["file"]
    home = home or home_dir()
    _state["home"] = home
    try:
        os.makedirs(home, exist_ok=True)
        p = log_path(home)
        if os.path.exists(p) and os.path.getsize(p) > 5 * 1024 * 1024:
            old = os.path.join(log_dir(home), "app.old.log")
            try:
                os.replace(p, old)
            except OSError:
                pass
        f = open(p, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return None
    _state["file"] = f
    sys.stdout = _Tee(sys.stdout, f)
    sys.stderr = _Tee(sys.stderr, f)
    try:
        faulthandler.enable(file=f, all_threads=True)          # hard crashes
    except Exception:
        pass

    def hook(et, ev, tb):
        line(f"UNCAUGHT ERROR:\n{''.join(traceback.format_exception(et, ev, tb))}")
    sys.excepthook = hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda a: line(
            f"UNCAUGHT ERROR in thread {getattr(a.thread, 'name', '?')}:\n"
            f"{''.join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback))}")
    line("=" * 70)
    line(f"Instrumental Studio starting  |  {platform.platform()}  |  Python {sys.version.split()[0]}  |  {sys.executable}")
    threading.Thread(target=_heartbeat, name="freeze-watch", daemon=True).start()
    return f


def append(msg, home=None):
    """Add one line to app.log without taking over the console (for the setup check and other tools)."""
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path(home), "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def line(msg):
    """One timestamped line in the log."""
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    try:
        tag = f" {_state['tag']}" if _state["tag"] else ""
        text = f"[{ts} +{time.time() - START:7.2f}s{tag}] {msg}"
        print(text, file=sys.stderr, flush=True)
        if any(w in msg for w in ERROR_WORDS) and "Traceback" not in msg:
            _to_errors(datetime.datetime.now().strftime("%Y-%m-%d ") + text)
    except Exception:
        pass


def mark(msg):
    line(f"startup: {msg}")


def _heartbeat():
    """Every half second: re-arm the C-level watchdog (it fires only if this thread can't run for
    FREEZE_S seconds, i.e. Python is blocked) and note how late this thread woke up."""
    f = _state["file"]
    last = time.time()
    while True:
        try:
            faulthandler.dump_traceback_later(FREEZE_S, repeat=False, file=f, exit=False)
        except Exception:
            return
        time.sleep(0.5)
        now = time.time()
        late = now - last - 0.5
        if late > 1.0:
            line(f"FREEZE: the app was blocked for about {late:.1f} s (what each thread was doing is "
                 f"dumped just above, if it lasted over {FREEZE_S:.0f} s)")
        last = now


def _about(extra):
    lines = [f"Created: {datetime.datetime.now().isoformat(timespec='seconds')}",
             f"Running for: {time.time() - START:.0f} s",
             f"OS: {platform.platform()}",
             f"Machine: {platform.machine()}  processors: {os.cpu_count()}",
             f"Python: {sys.version}",
             f"Executable: {sys.executable}"]
    for mod in ("numpy", "scipy", "torch", "webview", "demucs", "audio_separator"):
        m = sys.modules.get(mod)
        lines.append(f"{mod}: {getattr(m, '__version__', 'loaded') if m else 'not loaded'}")
    try:
        import shutil
        du = shutil.disk_usage(_state["home"] or home_dir())
        lines.append(f"Free disk: {du.free / 1e9:.1f} GB of {du.total / 1e9:.1f} GB")
    except Exception:
        pass
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("Threads right now:")
    for tid, frame in sys._current_frames().items():
        name = next((t.name for t in threading.enumerate() if t.ident == tid), str(tid))
        lines.append(f"--- {name}")
        lines.append("".join(traceback.format_stack(frame, limit=12)))
    return "\n".join(lines)


def export_bundle(folder, extra=None):
    """Zip app.log (+ the older copy) and a description of this install into `folder`. Returns the path.
    Contains no audio and no project data."""
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(folder, f"InstrumentalStudio-log-{stamp}.zip")
    home = _state["home"] or home_dir()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("app.log", "app.old.log", "errors.log"):
            p = os.path.join(log_dir(home), name)
            if os.path.exists(p):
                z.write(p, name)
        z.writestr("about.txt", _about(extra))
    line(f"log exported to {path}")
    return path
