"""Local HTTP server: JSON API + audio streaming + the UI files. Standard library only."""
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from . import separate as SEP
from . import updater as UP
from . import installer as INST
from . import __version__
from .audio import load_audio, wav_bytes, log
from .project import Project

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
HOME = os.environ.get("INSTRUMENTAL_STUDIO_HOME", os.path.join(os.path.expanduser("~"), "InstrumentalStudio"))
PROJECTS = os.path.join(HOME, "projects")
EXPORTS = os.path.join(HOME, "exports")
UPLOADS = os.path.join(HOME, "uploads")
AUDIO_EXT = (".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".aiff", ".aif", ".opus", ".wma")


class State:
    project = None
    rev = 0
    job = {"running": False, "progress": 0.0, "message": "", "error": None}
    audio_cache = {}
    lock = threading.RLock()
    update = {"info": None, "error": None, "checking": False, "installing": False, "progress": 0.0,
              "message": "", "installed": None, "auto": False}
    just_updated = False
    model = {"installed": False, "installing": False, "progress": 0.0, "message": "", "error": None,
             "device": "", "note": None}


S = State()


def _slug(name):
    base = re.sub(r"[^\w\- ]+", "", os.path.splitext(os.path.basename(name))[0]).strip() or "song"
    root, i = os.path.join(PROJECTS, base), 2
    while os.path.exists(root):
        root, i = os.path.join(PROJECTS, f"{base} ({i})"), i + 1
    return root


def _progress(f, m):
    if f is None:
        S.job.update(message=m)
    else:
        S.job.update(progress=float(f), message=m)


# ------------------------------------------------------------------ separation model install
_model_lock = threading.Lock()


def _probe_device():
    S.model["device"] = SEP.device_info()[1]


def start_model_install(force=False):
    """Install Demucs + PyTorch in the background (once). Safe to call repeatedly."""
    with _model_lock:
        if S.model["installing"] or (S.model["installed"] and not force):
            return
        S.model.update(installing=True, error=None, progress=0.0, message="Starting")

    def prog(f, m):
        if f is not None:
            S.model["progress"] = float(f)
        S.model["message"] = m

    def run():
        try:
            res = INST.install(prog)
            S.model.update(installed=True, note=res.get("note"))
            SEP._DEVICE = None
            _probe_device()
        except Exception as e:
            traceback.print_exc()
            S.model["error"] = str(e)
        finally:
            S.model["installing"] = False

    threading.Thread(target=run, daemon=True).start()


def _wait_for_model():
    """Used by a song job: install the model if needed and wait, mirroring its progress."""
    if SEP.demucs_available():
        return
    start_model_install()
    while S.model["installing"]:
        S.job.update(progress=0.02 + 0.3 * S.model["progress"],
                     message="Installing the separation model, one time only: " + (S.model["message"] or ""))
        time.sleep(0.5)
    if S.model["error"]:
        raise RuntimeError(S.model["error"])



def _start_job(fn, *args):
    if S.job["running"]:
        raise RuntimeError("Already working on a song — wait for it to finish.")
    S.job.update(running=True, progress=0.0, message="Starting", error=None)

    def run():
        try:
            fn(*args)
            S.job.update(progress=1.0, message="Ready")
        except Exception as e:
            traceback.print_exc()
            S.job.update(error=str(e), message="Failed")
        finally:
            S.job["running"] = False
            S.rev += 1
            S.audio_cache.clear()

    threading.Thread(target=run, daemon=True).start()


def _job_song(path, mode):
    root = _slug(path)
    p = Project(root)
    p.meta["name"] = os.path.splitext(os.path.basename(path))[0]
    first = not SEP.demucs_available()
    _wait_for_model()
    base = 0.32 if first else 0.02
    _progress(base, "Loading song")
    original, sr = load_audio(path)
    work = os.path.join(root, "work")
    os.makedirs(work, exist_ok=True)
    stems, sr = SEP.separate(path, work, mode,
                             lambda f, m: _progress(None if f is None else base + (0.52 - base) * f, m))
    shutil.rmtree(work, ignore_errors=True)
    p.meta["separation"] = SEP.MODELS[mode][0]
    p.set_stems(stems, sr, original, source=path)
    p.analyze(lambda f, m: _progress(0.53 + 0.47 * f, m))
    S.project = p


def _job_stems(folder, name):
    root = _slug(name)
    p = Project(root)
    p.meta["name"] = name
    _progress(0.02, "Loading stems")
    stems, sr = SEP.load_stem_folder(folder)
    p.meta["separation"] = "Imported stems"
    p.set_stems(stems, sr, None, source=folder)
    p.analyze(lambda f, m: _progress(0.05 + 0.95 * f, m))
    S.project = p


def _job_demo():
    from .demo import make_demo
    _progress(0.02, "Composing the demo song")
    damaged, original, _ = make_demo()
    p = Project(_slug("Demo song"))
    p.meta["name"] = "Demo song"
    p.meta["separation"] = "Simulated separator (demo)"
    p.set_stems(damaged, 44100, original, source="demo")
    p.analyze(lambda f, m: _progress(0.1 + 0.9 * f, m))
    S.project = p


def list_projects():
    out = []
    if os.path.isdir(PROJECTS):
        for d in sorted(os.listdir(PROJECTS)):
            pj = os.path.join(PROJECTS, d, "project.json")
            if os.path.exists(pj):
                try:
                    with open(pj) as f:
                        meta = json.load(f)["meta"]
                    out.append({"id": d, "name": meta.get("name", d), "duration": meta.get("duration"),
                                "modified": os.path.getmtime(pj)})
                except Exception:
                    pass
    return sorted(out, key=lambda x: -x["modified"])


def reveal(path):
    try:
        if platform.system() == "Windows":
            os.startfile(path)  # noqa
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the player cancelled a request (seek / version switch) — normal

    # --------------------------------------------------------------- helpers
    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _audio(self, data):
        """Serve WAV bytes with HTTP Range support so the player can seek."""
        total = len(data)
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            a = int(m.group(1)) if m.group(1) else 0
            b = int(m.group(2)) if m.group(2) else total - 1
            b = min(b, total - 1)
            chunk = data[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Range", f"bytes {a}-{b}/{total}")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(chunk)
        else:
            self._send(200, data, "audio/wav", {"Accept-Ranges": "bytes"})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self):
        b = self._body()
        return json.loads(b) if b else {}

    def _proj(self):
        if S.project is None:
            raise RuntimeError("No song is open.")
        return S.project

    def _edited(self):
        S.rev += 1
        S.audio_cache.clear()
        p = S.project
        threading.Thread(target=p.save, daemon=True).start()
        return {"ok": True, "rev": S.rev, "overview": p.overview()}

    # --------------------------------------------------------------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            path = u.path
            if path == "/" or not path.startswith("/api/"):
                f = "index.html" if path == "/" else path.lstrip("/")
                fp = os.path.normpath(os.path.join(UI_DIR, f))
                if not fp.startswith(UI_DIR) or not os.path.isfile(fp):
                    return self._send(404, "not found", "text/plain")
                ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                         ".svg": "image/svg+xml", ".png": "image/png"}.get(os.path.splitext(fp)[1], "application/octet-stream")
                with open(fp, "rb") as fh:
                    return self._send(200, fh.read(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
            if path == "/api/state":
                S.model["installed"] = S.model["installed"] or SEP.demucs_available()
                devname = S.model["device"]
                return self._send(200, {
                    "job": S.job, "rev": S.rev, "has_project": S.project is not None,
                    "project": S.project.meta.get("name") if S.project else None,
                    "demucs": SEP.demucs_available(), "device": devname,
                    "models": {k: v[0] for k, v in SEP.MODELS.items()}, "home": HOME,
                    "projects": list_projects(), "model": S.model, "version": __version__, "repo": UP.REPO,
                    "update": S.update, "update_prefs": UP.load_prefs(HOME), "just_updated": S.just_updated})
            if path == "/api/overview":
                return self._send(200, {"rev": S.rev, "overview": self._proj().overview()})
            if path == "/api/spectro":
                return self._send(200, self._proj().spectro(q["stem"], q.get("kind", "raw")), "application/octet-stream")
            if path == "/api/audio":
                p = self._proj()
                key = (S.rev, q.get("version", "repaired"), q.get("stem"))
                with S.lock:
                    if key not in S.audio_cache:
                        S.audio_cache.clear()
                        if q.get("stem"):
                            y = p.mix(q.get("version", "repaired"), solo_stem=q["stem"])
                        else:
                            y = p.mix(q.get("version", "repaired"))
                        S.audio_cache[key] = wav_bytes(y, p.sr)
                    data = S.audio_cache[key]
                return self._audio(data)
            if path == "/api/preview":
                p = self._proj()
                y = p.preview(q["id"], q.get("mode", "after"), solo=q.get("solo", "1") == "1")
                return self._audio(wav_bytes(y, p.sr))
            if path == "/api/candidates":
                return self._send(200, {"candidates": self._proj().donor_candidates(q["id"])})
            if path == "/api/report":
                return self._send(200, self._proj().report())
            return self._send(404, {"error": "unknown endpoint"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as e:
            traceback.print_exc()
            return self._send(400, {"error": str(e)})

    # --------------------------------------------------------------- POST
    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/upload":
                # raw body upload; X-Filename header; X-Batch groups several files (stem import)
                name = os.path.basename(urllib.parse.unquote(self.headers.get("X-Filename", "song.wav")))
                batch = re.sub(r"[^\w\-]", "", self.headers.get("X-Batch", "")) or str(int(time.time() * 1000))
                folder = os.path.join(UPLOADS, batch)
                os.makedirs(folder, exist_ok=True)
                dest = os.path.join(folder, name)
                remaining = int(self.headers.get("Content-Length") or 0)
                with open(dest, "wb") as fh:
                    while remaining > 0:
                        chunk = self.rfile.read(min(remaining, 1 << 20))
                        if not chunk:
                            break
                        fh.write(chunk)
                        remaining -= len(chunk)
                return self._send(200, {"ok": True, "path": dest, "folder": folder})
            d = self._json()
            if path == "/api/open_song":
                _start_job(_job_song, d["path"], d.get("mode", "4stem"))
                return self._send(200, {"ok": True})
            if path == "/api/open_stems":
                folder = d["folder"]
                _start_job(_job_stems, folder, d.get("name") or os.path.basename(folder.rstrip("/\\")))
                return self._send(200, {"ok": True})
            if path == "/api/demo":
                _start_job(_job_demo)
                return self._send(200, {"ok": True})
            if path == "/api/load":
                S.project = Project.load(os.path.join(PROJECTS, d["id"]))
                S.rev += 1
                S.audio_cache.clear()
                return self._send(200, {"ok": True})
            if path == "/api/delete_project":
                shutil.rmtree(os.path.join(PROJECTS, os.path.basename(d["id"])), ignore_errors=True)
                return self._send(200, {"ok": True})
            if path == "/api/model/install":
                start_model_install(force=bool(d.get("force")))
                return self._send(200, {"ok": True})
            if path == "/api/update/check":
                return self._send(200, do_check())
            if path == "/api/update/install":
                start_install()
                return self._send(200, {"ok": True})
            if path == "/api/update/prefs":
                prefs = UP.load_prefs(HOME)
                prefs.update({k: bool(v) for k, v in d.items() if k in ("check_on_start", "auto_install")})
                UP.save_prefs(HOME, prefs)
                return self._send(200, {"ok": True, "prefs": prefs})
            if path == "/api/update/restart":
                if S.job["running"]:
                    return self._send(400, {"error": "Wait for the current song to finish processing, then restart."})
                if S.project:
                    S.project.save()
                self._send(200, {"ok": True})
                threading.Timer(0.3, UP.relaunch).start()
                return
            p = self._proj()
            if path == "/api/status":
                p.set_status(d["ids"], d["status"])
            elif path == "/api/strength":
                p.set_strength(d["id"], d["strength"])
            elif path == "/api/donor":
                p.set_donor(d["id"], beat=d.get("beat"), time_s=d.get("time"))
            elif path == "/api/mixer":
                p.set_mixer(d)
            elif path == "/api/bulk":
                p.bulk(d["action"], d.get("stem"), d.get("section"), tuple(d.get("types", ["repair"])))
            elif path == "/api/undo":
                p.undo()
            elif path == "/api/settings":
                p.update_settings(d)
            elif path == "/api/rename_section":
                p.rename_section(d["index"], d["name"])
            elif path == "/api/export":
                folder = os.path.join(EXPORTS, re.sub(r"[^\w\- ()]", "", p.meta.get("name", "song")) or "song")
                files = p.export(folder, d.get("format", "wav"), tuple(d.get("what", ["mix", "stems", "vocals", "report"])))
                if d.get("reveal", True):
                    reveal(folder)
                return self._send(200, {"ok": True, "folder": folder, "files": files})
            elif path == "/api/reveal":
                reveal(d.get("path") or HOME)
                return self._send(200, {"ok": True})
            else:
                return self._send(404, {"error": "unknown endpoint"})
            return self._send(200, self._edited())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as e:
            traceback.print_exc()
            return self._send(400, {"error": str(e)})


def do_check():
    S.update.update(checking=True, error=None)
    try:
        S.update["info"] = UP.check()
    except Exception as e:
        S.update["error"] = str(e)
    finally:
        S.update["checking"] = False
    return S.update


def start_install(auto=False):
    if S.update["installing"]:
        return
    info = S.update.get("info")
    if not info or not info.get("available"):
        raise RuntimeError("Check for updates first.")
    S.update.update(installing=True, error=None, progress=0.0, message="Starting", auto=auto)

    def run():
        try:
            v = UP.install(info, lambda f, m: S.update.update(progress=f, message=m))
            S.update["installed"] = v
        except Exception as e:
            traceback.print_exc()
            S.update["error"] = str(e)
        finally:
            S.update["installing"] = False

    threading.Thread(target=run, daemon=True).start()


def _startup_update():
    prefs = UP.load_prefs(HOME)
    if not prefs.get("check_on_start") or S.just_updated:
        return
    do_check()
    info = S.update.get("info") or {}
    if info.get("available") and prefs.get("auto_install"):
        try:
            start_install(auto=True)
        except Exception as e:
            S.update["error"] = str(e)


def serve(port=0, just_updated=False):
    for d in (PROJECTS, EXPORTS, UPLOADS):
        os.makedirs(d, exist_ok=True)
    S.just_updated = just_updated
    threading.Thread(target=_startup_update, daemon=True).start()
    # get the separation model ready in the background so the first song doesn't wait for it
    if SEP.demucs_available():
        S.model["installed"] = True
        threading.Thread(target=_probe_device, daemon=True).start()
    elif UP.load_prefs(HOME).get("auto_install_model", True):
        start_model_install()
    for attempt in range(40):   # after an update restart the old copy may still hold the port briefly
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            if attempt == 39:
                raise
            time.sleep(0.25)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"
