"""Local HTTP server: JSON API + audio streaming + the UI files. Standard library only."""
import hmac
import json
import os
import secrets
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np  # noqa: F401

from . import separate as SEP
from . import updater as UP
from . import installer as INST
from . import __version__
from .audio import load_audio, wav_bytes, log
from . import proc as PR
from . import diag as DG


def _Project():
    """The song engine (NumPy/SciPy analysis) is loaded on first use, not before the window opens."""
    from .project import Project
    return Project



UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
HOME = os.environ.get("INSTRUMENTAL_STUDIO_HOME", os.path.join(os.path.expanduser("~"), "InstrumentalStudio"))
PROJECTS = os.path.join(HOME, "projects")
EXPORTS = os.path.join(HOME, "exports")
UPLOADS = os.path.join(HOME, "uploads")

# ----------------------------------------------------------------------------- lockdown
# The server only listens on this computer, but any web page open in a browser on this computer can
# still send requests to it. So:
#  - every request for data (GET) and every change (POST) must carry this session's secret, which only the
#    app's own window knows: it arrives in the address fragment (never sent to a server, never in a page),
#    then travels as a header, or as a SameSite=Strict, HttpOnly cookie for audio the player streams.
#    Other programs and user accounts on this computer can reach 127.0.0.1 too, so the page itself holds
#    no secret,
#  - requests must name 127.0.0.1/localhost and this port as the Host (stops DNS-rebinding tricks),
#  - requests from other sites are turned away using the browser's Origin / Sec-Fetch-Site headers,
#  - the app's pages can't be framed by other sites, and its audio can't be embedded by them,
#  - "Show folder" only opens folders inside the app's own folder, and never runs files.
# The secret comes from the window process (environment, removed at once so helpers never inherit it); run on
# its own, the engine makes one and logs the address with it.
TOKEN = os.environ.pop("STUDIO_TOKEN", "") or secrets.token_urlsafe(32)
COOKIE = "studio_s"
MAX_UPLOAD = 4 << 30           # 4 GB per uploaded file
LOCAL_NAMES = ("127.0.0.1", "localhost")
MAX_JSON = 16 << 20            # 16 MB: far more than any real request needs
SEC_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _inside(path, folder):
    try:
        path, folder = os.path.realpath(path), os.path.realpath(folder)
        return os.path.commonpath([path, folder]) == folder
    except (ValueError, OSError):
        return False
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
    extras = {}
    model = {"installed": False, "installing": False, "progress": 0.0, "message": "", "error": None,
             "device": "", "note": None, "plan": None}


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
    try:
        dev, name = SEP.device_info()
        S.model["device"] = name
        S.model["device_kind"] = dev
    except Exception:
        S.model["device"] = "unknown (couldn't check)"
        S.model["device_kind"] = "unknown"
    try:
        S.model["nvidia"] = INST.has_nvidia()
    except Exception:
        S.model["nvidia"] = False
    try:
        S.model["gpus"] = INST.gpu_names()
        DG.line(f"graphics cards: {', '.join(S.model['gpus']) or 'none found'}"
                + (f" (nvidia-smi: {INST.SMI_PROBLEM[0]})" if INST.SMI_PROBLEM[0] else ""))
    except Exception:
        S.model["gpus"] = []
    DG.line(f"splitting runs on: {S.model['device']}" + (" (an NVIDIA card is present but not used: Settings, "
                                                          "Use my graphics card)" if S.model["nvidia"] and
                                                          S.model.get("device_kind") == "cpu" else ""))


def start_model_install(force=False, gpu=False):
    """Install Demucs + PyTorch in the background (once). Safe to call repeatedly."""
    with _model_lock:
        if S.model["installing"] or (S.model["installed"] and not force):
            return
        if not S.job.get("running"):
            PR.CANCEL.clear()          # an old Stop must not stop a download started afterwards
        S.model.update(installing=True, error=None, progress=0.0, message="Starting")

    def prog(f, m):
        if f is not None:
            S.model["progress"] = float(f)
        S.model["message"] = m

    def run():
        try:
            res = INST.install(prog, gpu=gpu)
            S.model.update(installed=True, note=res.get("note"))
            SEP._DEVICE = None
            _probe_device()
        except PR.Stopped:
            S.model["error"] = "Stopped. Click Try again to start the download again."
        except Exception as e:
            traceback.print_exc()
            S.model["error"] = str(e)
        finally:
            try:
                S.model["installed"] = INST.installed(fresh=True)     # a stopped switch may have left it half done
            except Exception:
                pass
            S.model["installing"] = False

    threading.Thread(target=run, daemon=True).start()


def _model_plan():
    if S.model["plan"] is None:
        try:
            p = INST.plan()
            S.model["plan"] = {"size": p["size"], "runs_on": p["runs_on"], "note": p.get("note")}
        except Exception:
            S.model["plan"] = {"size": "about 1-3 GB", "runs_on": "your computer", "note": None}
    return S.model["plan"]


_extras_checked = [0.0]


def extras_state():
    fresh = time.time() - _extras_checked[0] > 10          # checking the disk on every poll isn't needed
    for key, e in INST.EXTRAS.items():
        st = S.extras.setdefault(key, {"installing": False, "progress": 0.0, "message": "", "error": None})
        st.update(label=e["label"], what=e["what"], size=e["size"])
        if not st["installing"] and (fresh or "installed" not in st):
            st["installed"] = INST.extra_installed(key)
    if fresh:
        _extras_checked[0] = time.time()
    return S.extras


# ------------------------------------------------------------------ troubleshooting
MARKER = os.path.join(HOME, ".starting")


def stop_everything():
    PR.CANCEL.set()
    n = PR.kill_all()
    for st in S.extras.values():
        if st.get("installing"):
            st.update(installing=False, error="Stopped.")
    if S.model.get("installing"):
        S.model.update(installing=False, error="Stopped. Click Try again to start the download again.")
    return n


def clear_model_files():
    """Delete downloaded separation model weights (they download again on the next split).
    Leaves other programs' files alone: only Demucs checkpoints (*.th) and this app's own models folder."""
    removed = 0
    roots = [os.environ.get("TORCH_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "torch")]
    if os.name == "nt":
        roots.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "torch"))
    for r in roots:
        ck = os.path.join(r, "hub", "checkpoints")
        if os.path.isdir(ck):
            for f in os.listdir(ck):
                if f.endswith(".th") or f.endswith(".th.partial") or f.endswith(".partial"):
                    try:
                        os.remove(os.path.join(ck, f))
                        removed += 1
                    except OSError:
                        pass
    mine = os.path.join(HOME, "models")
    if os.path.isdir(mine):
        removed += sum(len(fs) for _, _, fs in os.walk(mine))
        shutil.rmtree(mine, ignore_errors=True)
    return removed


def start_extra_install(key):
    st = extras_state()[key]
    if st["installing"] or st.get("installed"):
        return
    st.update(installing=True, error=None, progress=0.0, message="Starting")
    if not S.job.get("running"):
        PR.CANCEL.clear()

    def prog(f, m):
        if f is not None:
            st["progress"] = float(f)
        st["message"] = m

    def run():
        try:
            INST.install_extra(key, prog)
            st["installed"] = True
        except Exception as e:
            traceback.print_exc()
            st["error"] = str(e)
        finally:
            st["installing"] = False

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
    PR.CANCEL.clear()
    S.job.update(running=True, progress=0.0, message="Starting", error=None, result=None, started=time.time(),
                 stopped=False)

    def run():
        try:
            fn(*args)
            S.job.update(progress=1.0, message="Ready")
        except PR.Stopped:
            S.job.update(error=None, stopped=True, message="Stopped")
        except Exception as e:
            traceback.print_exc()
            S.job.update(error=str(e), message="Failed")
        finally:
            S.job["running"] = False
            S.rev += 1
            S.audio_cache.clear()

    threading.Thread(target=run, daemon=True).start()


def _job_song(path, mode=None):
    """Step 1 only: bring the song into a project so it can be played. Splitting comes later."""
    root = _slug(path)
    p = _Project()(root)
    p.meta["name"] = os.path.splitext(os.path.basename(path))[0]
    _progress(0.05, "Loading song")
    original, sr = load_audio(path)
    os.makedirs(root, exist_ok=True)
    keep = os.path.join(root, "source" + os.path.splitext(path)[1].lower())
    shutil.copyfile(path, keep)
    p.meta["source_hash"] = _file_hash(keep)
    p.set_original(original, sr, source=keep, progress=lambda f, m: _progress(0.2 + 0.8 * f, m))
    S.project = p


def _split_estimates():
    """Rough seconds each split mode would take on this song and computer ({} with no song open)."""
    p = S.project
    if not p or p.original is None:
        return {}
    dev = S.model.get("device_kind") or "cpu"
    rate = (UP.load_prefs(HOME).get("split_speed") or {}).get(dev) or SEP.DEFAULT_SPEED.get(dev, 0.8)
    dur = p.original.shape[1] / p.sr
    return {k: round(dur * rate * SEP.passes(k)) for k in SEP.MODELS}


def _job_split(mode, opts=None):
    """Step 2: split into instruments (or reuse an earlier split of the same file), then map the song."""
    p = S.project
    src = p.meta.get("source")
    if src == "demo":
        import numpy as np
        _progress(0.1, "Splitting the demo song")
        z = np.load(os.path.join(p.root, "demo_stems.npz"))
        stems, sr = {k: z[k] for k in z.files}, p.sr
        p.meta["separation"] = "Simulated separator (demo)"
    else:
        key = mode + ("+backing" if (opts or {}).get("backing") else "") + ("+drums" if (opts or {}).get("drums") else "")
        stems = _reuse_stems(p.meta.get("source_hash"), key, exclude=p.root)
        sr = p.sr
        if stems:
            _progress(0.5, "Reusing the split from your earlier project of this song")
        else:
            work = os.path.join(p.root, "work")
            os.makedirs(work, exist_ok=True)
            t0 = time.time()
            stems, sr = SEP.separate(src, work, mode, lambda f, m: _progress(None if f is None else 0.02 + 0.8 * f, m), opts)
            took = time.time() - t0
            shutil.rmtree(work, ignore_errors=True)
            dev = S.model.get("device_kind") or "cpu"
            p.meta["split_info"] = {"device": S.model.get("device") or "processor", "kind": dev, "seconds": round(took, 1)}
            try:                       # remember this computer's speed, for estimates next time
                rate = took / max(1.0, p.original.shape[1] / p.sr) / SEP.passes(mode, opts)
                prefs = UP.load_prefs(HOME)
                sp = prefs.get("split_speed") or {}
                sp[dev] = round(0.5 * sp[dev] + 0.5 * rate, 4) if dev in sp else round(rate, 4)
                prefs["split_speed"] = sp
                UP.save_prefs(HOME, prefs)
            except Exception as e:
                log(f"split speed not saved: {e}")
        extra = [w for w, on in (("backing vocals on their own track", (opts or {}).get("backing")),
                                 ("drums split into kick, snare, toms and cymbals", (opts or {}).get("drums"))) if on]
        p.meta["separation"] = SEP.MODELS[mode][0] + (", " + ", ".join(extra) if extra else "")
    p.meta["sep_mode"] = mode if src == "demo" else key
    p.set_stems(stems, sr, p.original, source=src)
    p.prepare(lambda f, m: _progress(None if f is None else 0.85 + 0.15 * f, m))


def _job_alt(mode, start, end, stem, blend=0.0):
    """Split the song with another model (once; kept with the project), then offer it for one part."""
    p = S.project
    if mode not in p.alt_modes():
        stems = _reuse_stems(p.meta.get("source_hash"), mode, exclude=p.root)
        if stems:
            _progress(0.5, "Reusing an earlier split with that model")
        else:
            work = os.path.join(p.root, "work_alt")
            os.makedirs(work, exist_ok=True)
            stems, _ = SEP.separate(p.meta["source"], work, mode,
                                    lambda f, m: _progress(None if f is None else 0.02 + 0.9 * f, m))
            shutil.rmtree(work, ignore_errors=True)
        n = p.original.shape[1]
        p.set_alt(mode, {k: (v[:, :n] if v.shape[1] >= n else np.pad(v, ((0, 0), (0, n - v.shape[1]))))
                         for k, v in stems.items()})
    _progress(0.95, "Preparing the comparison")
    ids, skipped = p.make_alt(mode, start, end, stem, SEP.MODELS[mode][0], blend)
    S.job["result"] = {"new_ids": ids, "skipped": skipped}
    p.save()


def _job_detect():
    S.project.detect(lambda f, m: _progress(None if f is None else f, m))


def _deep_clean(p, f0=0.0, f1=1.0):
    """Second vocal-removal pass: run the vocal model on each instrument (not drums) and offer to take out
    what it finds, where it moves like the singer. Returns the new review ids."""
    if not SEP.demucs_available():
        raise RuntimeError("The second pass needs the separation model. Install it from the start screen.")
    from .project import NOT_REPAIRED
    targets = [k for k in p.instruments if k not in NOT_REPAIRED and k not in p.PERCUSSIVE]
    use_rof = bool(UP.load_prefs(HOME).get("roformer_passes"))       # off by default: Roformer is much slower
    ids = []
    for i, k in enumerate(targets):
        a = f0 + (f1 - f0) * i / max(1, len(targets))
        b = f0 + (f1 - f0) * (i + 1) / max(1, len(targets))
        work = os.path.join(p.root, "work_deep", k)
        try:
            found = SEP.vocals_in(p.stems[k], p.sr, work, lambda f, m: _progress(None if f is None else f, m), a, b,
                                  f"Second pass on the {k}", roformer=use_rof)
            ids += p.add_deep_leftovers(k, found)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(os.path.join(p.root, "work_deep"), ignore_errors=True)
    return ids


def _recover(p, f0=0.0, f1=1.0):
    """Find the instrument sound in the vocal track (one fast model run, kept with the song so it never runs
    twice) and offer it back. Returns the new review ids."""
    import numpy as np
    cache = os.path.join(p.root, "stems", "_recovered.npy")
    rec = None
    if os.path.isfile(cache):
        try:
            rec = np.load(cache)
        except Exception:
            rec = None
    if rec is None or rec.shape[1] != p.stems["vocals"].shape[1]:
        if not SEP.demucs_available():
            raise RuntimeError("Bringing back details needs the separation model.")
        work = os.path.join(p.root, "work_recover")
        try:
            rec = SEP.instruments_in(p.stems["vocals"], p.sr, work,
                                     lambda f, m: _progress(None if f is None else f, "Looking for instruments in the vocal track"),
                                     f0, f1)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        np.save(cache, rec.astype(np.float32))
    return p.add_recovered(rec)


def _pre_build(p, steps, f0, f1):
    """Model steps that run before the build itself: recover (once per song), then the optional second pass."""
    mid = f0 + (f1 - f0) * (0.5 if "deep" in steps else 1.0)
    if "recover" in steps:
        try:
            _recover(p, f0, mid)
        except Exception as e:
            log(f"build: recovering details skipped: {e}")
    if "deep" in steps:
        try:
            _deep_clean(p, mid, f1)
        except Exception as e:
            log(f"build: second pass skipped: {e}")


def _job_import_ref(todo, kind, apply):
    p = S.project
    report, all_ids, stems = [], [], set()
    for i, (src, stem, name, use) in enumerate(todo):
        f0 = i / len(todo)
        _progress(0.05 + 0.85 * f0, f"Reading {name}")
        try:
            audio = load_audio(src, p.sr)[0]
            _progress(0.05 + 0.85 * (f0 + 0.4 / len(todo)), f"Working out where {name} goes")
            how = kind
            if kind == "auto":
                dur, song = audio.shape[1] / p.sr, p.original.shape[1] / p.sr
                bar = 60.0 / max(1e-3, float(np.median(np.diff(p.beat_times)))) if len(p.beat_times) > 1 else 120.0
                bar_s = 4 * 60.0 / bar
                how = "track" if dur >= min(max(20.0, 0.35 * song), 0.9 * song) else ("loop" if dur >= 0.4 * bar_s else "sample")
            if how == "track" and stem in ("", "auto", None):
                stem, val = p.identify_track(audio)
                if not stem:
                    raise ValueError("couldn't tell which instrument it is; choose it in the list and add it again.")
            if how == "loop":
                ids, what = p.add_reference_loop(audio, name, use, None if stem in ("", "auto", None) else stem)
                report.append(f"{name}: {what}; used in {len(ids)} part{'s' if len(ids) != 1 else ''}.")
            elif how == "track":
                ids = p.add_reference_track(audio, stem, name, use)
                stems.add(stem)
                where = "where the singing damaged it" if use == "vocals" else "for the whole track"
                report.append(f"{name}: lined up with the {stem} ({p.meta['refs'][stem]['match']:.0%} match), used {where} "
                              f"in {len(ids)} part{'s' if len(ids) != 1 else ''}.")
            else:
                ids, what = p.add_reference_sample(audio, name)
                report.append(f"{name}: a one-shot, matched to {what}; rebuilds {len(ids)} part{'s' if len(ids) != 1 else ''}.")
            all_ids += ids
        except ValueError as e:
            report.append(f"{name}: {e}")
    _progress(0.92, "Checking the other fixes against your clean tracks")
    p.check_ids(all_ids)
    others = [i for i, x in p.suggestions.items() if x["stem"] in stems and i not in all_ids
              and x["status"] in ("pending", "accepted", "auto")]
    p.check_ids(others)
    misfit = [i for i in others if p.flagged(i)]
    good = [i for i in all_ids if not p.flagged(i)]
    if apply and good:
        p.set_status(good, "accepted", f"Used your clean track{'s' if len(todo) != 1 else ''}")
        report.append(f"Applied {len(good)} part{'s' if len(good) != 1 else ''}. Undo (Ctrl+Z) takes them back.")
    elif all_ids:
        report.append(f"Added {len(all_ids)} part{'s' if len(all_ids) != 1 else ''} to Review to hear before applying.")
    if misfit:
        report.append(f"{len(misfit)} other fix{'es' if len(misfit) != 1 else ''} on those instruments add sound your "
                      f"clean track doesn't have; they're under Doesn't fit.")
    p.save()
    S.job["result"] = {"title": "Your files are in",
                       "new_ids": all_ids, "report": report}


def _job_deep():
    p = S.project
    ids = _deep_clean(p, 0.02, 0.95)
    p.check_ids(ids)
    p.save()
    S.job["result"] = {"title": "Second pass done", "new_ids": ids,
                       "report": [f"Found leftover singing in {len(ids)} part{'s' if len(ids) != 1 else ''} of the instruments. "
                                  f"They're in Review under Vocal leftovers, to hear before applying."] if ids else
                                 ["The second pass found no leftover singing that moves like the singer."]}


def _job_build(steps):
    p = S.project
    pre = "deep" in steps or ("recover" in steps and not any(x.get("recover") for x in p.suggestions.values()))
    _pre_build(p, steps, 0.0, 0.4)
    res = p.build_best(steps, lambda f, m: _progress(None if f is None else (0.4 + 0.6 * f if pre else f), m))
    _progress(0.97, "Saving")
    p.save()
    S.job["result"] = {"report": res["report"], "title": "Your instrumental is built"}


def _file_hash(path):
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _reuse_stems(digest, mode, exclude=None):
    """Stems from an earlier project of the same file split with the same model, or None."""
    import numpy as np
    if not digest:
        return None
    for d in os.listdir(PROJECTS) if os.path.isdir(PROJECTS) else []:
        if exclude and os.path.abspath(os.path.join(PROJECTS, d)) == os.path.abspath(exclude):
            continue
        pj = os.path.join(PROJECTS, d, "project.json")
        try:
            with open(pj, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(65536)        # meta comes first; never parse the whole (large) file
        except OSError:
            continue
        if f'"source_hash": "{digest}"' not in head or f'"sep_mode": "{mode}"' not in head:
            continue
        sd = os.path.join(PROJECTS, d, "stems")
        try:
            stems = {f[:-4]: np.load(os.path.join(sd, f)) for f in os.listdir(sd)
                     if f.endswith(".npy") and not f.startswith("_")}
        except OSError:
            continue
        if stems and "vocals" in stems:     # a song whose split was cleared has none left to reuse
            return stems
    return None


def _auto_rest(p):
    """After a split or a stem import: find the damage, then build the best instrumental with its usual steps."""
    p.detect(lambda f, m: _progress(None if f is None else 0.3 * f, f"Finding damage: {m}"))
    steps = [x["key"] for x in p.build_plan() if x["on"]]
    _pre_build(p, steps, 0.3, 0.55)
    res = p.build_best(steps, lambda f, m: _progress(None if f is None else 0.55 + 0.42 * f, m))
    _progress(0.98, "Saving")
    p.save()
    S.job["result"] = {"report": res["report"], "title": "Your instrumental is built"}


def _job_full(mode, opts):
    """One click for a song: split it, find the damage, build the best instrumental."""
    _job_split(mode, opts)
    _auto_rest(S.project)


# ------------------------------------------------------------------ vinyl crackle
FRESH_MIXER = {"stem_gain": {}, "mute": {}, "solo": {}, "section_gain": {}, "keep": [], "vocal_lift": {},
               "mute_ranges": [], "sides": [], "mud_cut": {}, "tone_eq": {}, "seams": [], "eq_ranges": []}


def _use_recording(p, audio, sr, source, raw, vinyl):
    """Make `audio` (saved at `source`) the song's recording. A split made from the previous recording no
    longer matches, so it's cleared; returns that split's settings so it can be done again."""
    import json as _json
    was = p.meta.get("sep_mode") if p.stage != "imported" else None
    sd = os.path.join(p.root, "stems")
    if os.path.isdir(sd):
        for f in os.listdir(sd):
            if f.endswith(".npy") and not f.startswith("_"):
                os.remove(os.path.join(sd, f))
    shutil.rmtree(os.path.join(p.root, "alt"), ignore_errors=True)
    p.stems, p.take_w, p.history = {}, {}, []
    for k in list(p.__dict__):
        if k in ("_render_cache",):
            p._render_cache = {}
        elif k in ("_sounds_cache", "_dscale", "_guard", "_metric_cache", "_alt_cache", "_render_gen", "stem_feats",
                   "stem_env", "inst_sum", "_tail"):
            p.__dict__.pop(k, None)
    for k in ("sep_mode", "separation", "analyzed", "quality", "quality_before", "verified", "grid_drift"):
        p.meta.pop(k, None)
    base = p.meta.get("source_hash_raw") or p.meta.get("source_hash") or ""
    p.meta["source_hash_raw"] = base
    if vinyl:
        p.meta.update(source_raw=raw, vinyl=vinyl, source_hash=f"{base}:vinyl:{vinyl['strength']}")
    else:
        p.meta.pop("source_raw", None)
        p.meta.pop("vinyl", None)
        p.meta["source_hash"] = base
    p.mixer = _json.loads(_json.dumps(FRESH_MIXER))
    p.set_original(audio, sr, source=source, progress=lambda f, m: _progress(0.66 + 0.04 * f, m))
    return was


def _split_again(p, key):
    if not key or p.meta.get("source") == "demo":
        return []
    mode = key.split("+")[0]
    opts = {"backing": "+backing" in key, "drums": "+drums" in key}
    _progress(0.7, "Splitting the cleaned recording again")
    _job_split(mode, opts)
    return [f"Split again with the same settings ({SEP.MODELS.get(mode, ('',))[0]}). Run Find damage again, "
            f"or Build the best instrumental."]


def _job_vinyl(strength):
    from . import vinyl as VN
    from .audio import save_audio
    p = S.project
    raw = p.meta.get("source_raw") or p.meta.get("source")
    if not raw or raw == "demo" or not os.path.isfile(raw):
        raise RuntimeError("Only a song opened from a file can be cleaned (not imported stems or the demo).")
    _progress(0.03, "Reading the recording")
    x, sr = load_audio(raw)                      # always the untouched file, so strengths never stack
    y, n = VN.declick(x, sr, strength, progress=lambda f: _progress(0.05 + 0.6 * f, f"Removing clicks and crackle ({strength})"))
    out = os.path.join(p.root, f"source_declicked_{strength}.wav")
    save_audio(out, y, sr, bits=32)
    key = _use_recording(p, y, sr, out, raw, {"strength": strength, "clicks": int(n)})
    lines = [f"Removed {n} clicks and crackles ({strength}). The untouched recording is kept: Undo crackle removal "
             f"brings it back."] + _split_again(p, key)
    p.save()
    S.job["result"] = {"report": lines, "title": "Vinyl crackle removed"}


def _job_declick_stems(strength):
    p = S.project
    ids = p.declick_stems(strength, lambda f, m: _progress(0.02 + 0.93 * f, m))
    p.save()
    S.job["result"] = {"title": "Crackle found in the instruments", "new_ids": ids,
                       "report": [f"Found vinyl clicks and crackle in {len(ids)} part{'s' if len(ids) != 1 else ''} of the "
                                  f"instruments ({strength}). They're in Review under Clicks and smoothing, to hear before "
                                  f"applying; Build the best instrumental applies them too."] if ids else
                                 ["No vinyl clicks or crackle found in the instruments at this strength."]}


def _job_vinyl_undo():
    p = S.project
    raw = p.meta.get("source_raw")
    if not raw or not os.path.isfile(raw):
        raise RuntimeError("The untouched recording isn't there any more.")
    _progress(0.05, "Reading the untouched recording")
    x, sr = load_audio(raw)
    key = _use_recording(p, x, sr, raw, raw, None)
    lines = ["Back to the untouched recording."] + _split_again(p, key)
    p.save()
    S.job["result"] = {"report": lines, "title": "Crackle removal undone"}


def _job_stems(folder, name, auto=False):
    root = _slug(name)
    p = _Project()(root)
    p.meta["name"] = name
    _progress(0.02, "Loading stems")
    stems, sr = SEP.load_stem_folder(folder)
    p.meta["separation"] = "Imported stems"
    p.set_stems(stems, sr, None, source=folder)
    p.prepare(lambda f, m: _progress(None if f is None else 0.05 + 0.95 * f, m))
    S.project = p
    if auto:
        _auto_rest(p)


def _job_demo():
    from .demo import make_demo
    _progress(0.02, "Composing the demo song")
    damaged, original, _ = make_demo()
    import numpy as np
    p = _Project()(_slug("Demo song"))
    p.meta["name"] = "Demo song"
    os.makedirs(p.root, exist_ok=True)
    np.savez(os.path.join(p.root, "demo_stems.npz"), **damaged)   # what "Split" will use for the demo
    p.set_original(original, 44100, source="demo", progress=lambda f, m: _progress(0.5 + 0.5 * f, m))
    S.project = p


_project_meta = {}      # project folder -> (modified time, listing row)


def _peek_meta(pj):
    """Name and length of a song from the start of its project.json, without parsing the whole file
    (it holds every repair and can be tens of MB; parsing it all froze the window at startup)."""
    with open(pj, "r", encoding="utf-8", errors="replace") as f:
        head = f.read(65536)
    name = re.search(r'"name":\s*"((?:[^"\\]|\\.)*)"', head)
    dur = re.search(r'"duration":\s*([0-9.eE+-]+)', head)
    try:
        nm = json.loads(f'"{name.group(1)}"') if name else None
    except ValueError:
        nm = None
    try:
        du = float(dur.group(1)) if dur else None
    except ValueError:
        du = None
    return nm, du


def list_projects():
    """Rows for the Songs list. Reads each song's small summary.json (written on save); older songs
    without one are peeked at, never fully parsed. Results are kept until the song changes."""
    out = []
    if os.path.isdir(PROJECTS):
        for d in sorted(os.listdir(PROJECTS)):
            folder = os.path.join(PROJECTS, d)
            pj = os.path.join(folder, "project.json")
            try:
                mt = os.path.getmtime(pj)
            except OSError:
                continue
            hit = _project_meta.get(folder)
            if hit and hit[0] == mt:
                out.append(hit[1])
                continue
            name, dur = None, None
            try:
                with open(os.path.join(folder, "summary.json")) as f:
                    sm = json.load(f)
                name, dur = sm.get("name"), sm.get("duration")
            except (OSError, ValueError):
                try:
                    name, dur = _peek_meta(pj)
                except OSError:
                    pass
            row = {"id": d, "name": name or d, "duration": dur, "modified": mt}
            _project_meta[folder] = (mt, row)
            out.append(row)
    return sorted(out, key=lambda x: -x["modified"])


def reveal(path):
    """Open a folder in the file manager. Only folders inside the app's own folder: on Windows,
    opening a file would run it, so files are shown selected in their folder instead."""
    path = os.path.realpath(path or HOME)
    bh = UP.backup_home()
    if not (_inside(path, HOME) or _inside(path, DG.log_dir()) or (bh and os.path.realpath(bh) == path)) \
            or not os.path.exists(path):
        log(f"reveal refused: {path}")
        return
    try:
        if os.path.isfile(path):
            if platform.system() == "Windows":
                subprocess.Popen(["explorer", "/select,", path])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        elif platform.system() == "Windows":
            os.startfile(path)  # noqa  (a folder: opens Explorer)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


_save_state = {"lock": threading.Lock(), "pending": set()}


def _schedule_save(p):
    """Save after an edit, merging bursts: clicking through ten repairs saves once or twice, not ten
    times in parallel threads (each used to rewrite every file while holding the song)."""
    with _save_state["lock"]:
        if id(p) in _save_state["pending"]:
            return
        _save_state["pending"].add(id(p))

    def run():
        time.sleep(0.4)
        with _save_state["lock"]:
            _save_state["pending"].discard(id(p))
        try:
            p.save()
        except Exception:
            traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 120          # a connection that sends nothing for 2 minutes is closed (keeps idle sockets from piling up)

    def log_message(self, fmt, *args):
        pass

    def parse_request(self):
        # the clock starts once a request has arrived: a kept-open connection can sit idle for hours
        # between requests, and counting that made the log report "slow requests" that weren't
        self._t0 = time.time()
        return super().parse_request()

    def handle_one_request(self):
        self._t0 = None
        super().handle_one_request()
        if self._t0 is None:
            return
        dt = time.time() - self._t0
        if dt > 1.0 and getattr(self, "path", None):
            DG.line(f"slow request: {getattr(self, 'command', '?')} {self.path.split('?')[0]} took {dt:.1f} s")

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
        for k, v in SEC_HEADERS.items():
            self.send_header(k, v)
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
            for k, v in SEC_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(chunk)
        else:
            self._send(200, data, "audio/wav", {"Accept-Ranges": "bytes"})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_JSON:
            raise ValueError("Request too large.")
        return self.rfile.read(n) if n else b""

    # --------------------------------------------------------------- lockdown checks
    def _host_ok(self):
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "").strip().lower() in {f"{h}:{port}" for h in LOCAL_NAMES}

    def _from_other_site(self):
        """True when the browser says this request came from another web page."""
        port = self.server.server_address[1]
        origin = self.headers.get("Origin")
        if origin is not None and origin.rstrip("/").lower() not in {f"http://{h}:{port}" for h in LOCAL_NAMES}:
            return True                       # includes "null" (sandboxed frames, file:// pages)
        return (self.headers.get("Sec-Fetch-Site") or "").lower() in ("cross-site", "same-site")

    def _token_ok(self):
        return hmac.compare_digest(self.headers.get("X-Studio-Token") or "", TOKEN)

    def _cookie_ok(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v, TOKEN):
                return True
        return False

    def _refuse(self, why):
        log(f"refused {self.command} {self.path}: {why}")
        self.close_connection = True
        return self._send(403, {"error": "Not allowed."})

    def do_OPTIONS(self):
        # never approve cross-site requests (CORS preflights)
        return self._refuse("preflight")

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
        _schedule_save(p)
        return {"ok": True, "rev": S.rev, "overview": p.overview()}

    # --------------------------------------------------------------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            path = u.path
            if not self._host_ok():
                return self._refuse("host")
            if path.startswith("/api/") and self._from_other_site():
                return self._refuse("other site")
            if path == "/" or not path.startswith("/api/"):
                f = "index.html" if path == "/" else path.lstrip("/")
                fp = os.path.normpath(os.path.join(UI_DIR, f))
                if not _inside(fp, UI_DIR) or not os.path.isfile(fp):
                    return self._send(404, "not found", "text/plain")
                ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                         ".svg": "image/svg+xml", ".png": "image/png"}.get(os.path.splitext(fp)[1], "application/octet-stream")
                with open(fp, "rb") as fh:
                    data = fh.read()
                return self._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
            if not (self._token_ok() or self._cookie_ok()):
                return self._refuse("token")
            if path == "/api/state":
                have = SEP.demucs_available()          # cached for 30 s: polls must answer instantly
                S.model["installed"] = S.model["installed"] or have
                devname = S.model["device"]
                return self._send(200, {
                    "job": S.job, "rev": S.rev, "has_project": S.project is not None,
                    "project": S.project.meta.get("name") if S.project else None,
                    "demucs": have, "device": devname,
                    "models": {k: v[0] for k, v in SEP.MODELS.items()}, "home": HOME,
                    "projects": list_projects(), "model": S.model, "extras": extras_state(), "version": __version__, "repo": UP.REPO,
                    "backup_home": UP.backup_home(), "app_dir": UP.APP_DIR,
                    "split_estimates": _split_estimates(),
                    "python": {"version": sys.version.split()[0], "final": sys.version_info.releaselevel == "final"},
                    "update": S.update, "update_prefs": UP.load_prefs(HOME), "just_updated": S.just_updated,
                    "safe": getattr(S, "safe", False)})
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
                hear = q.get("hear") or ("solo" if q.get("solo", "0") == "1" else "mix")
                y = p.preview(q["id"], q.get("mode", "after"), hear=hear if hear in ("solo", "mix", "song") else "mix")
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
            if not self._host_ok():
                return self._refuse("host")
            if self._from_other_site():
                return self._refuse("other site")
            if not self._token_ok():
                return self._refuse("token")
            if path == "/api/session":
                # lets the audio player (which can't send headers) prove it's the app: strict same-site cookie
                return self._send(200, {"ok": True}, extra={
                    "Set-Cookie": f"{COOKIE}={TOKEN}; HttpOnly; SameSite=Strict; Path=/"})
            if path == "/api/upload":
                # raw body upload; X-Filename header; X-Batch groups several files (stem import)
                name = os.path.basename(urllib.parse.unquote(self.headers.get("X-Filename", "song.wav")))
                batch = re.sub(r"[^\w\-]", "", self.headers.get("X-Batch", "")) or str(int(time.time() * 1000))
                folder = os.path.join(UPLOADS, batch)
                os.makedirs(folder, exist_ok=True)
                dest = os.path.join(folder, name)
                remaining = int(self.headers.get("Content-Length") or 0)
                if remaining < 0 or remaining > MAX_UPLOAD:
                    return self._send(413, {"error": "That file is too big (4 GB at most)."})
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
                _start_job(_job_song, d["path"])
                return self._send(200, {"ok": True})
            if path == "/api/split":
                p = self._proj()
                mode = d.get("mode", "4stem")
                if (p.meta.get("source") != "demo" and not SEP.demucs_available()
                        and not _reuse_stems(p.meta.get("source_hash"), mode, exclude=p.root)):
                    return self._send(409, {"error": "need_model", "plan": _model_plan()})
                opts = {"backing": bool(d.get("backing")), "drums": bool(d.get("drums"))}
                if p.meta.get("source") != "demo":
                    need = {SEP.NEEDS_EXTRA.get(mode)} | ({"roformer"} if opts["backing"] else set()) \
                        | ({"drumsep"} if opts["drums"] else set())
                    missing = [k for k in need - {None} if not INST.extra_installed(k)]
                    if missing:
                        return self._send(409, {"error": "need_extra", "extras": missing})
                _start_job(_job_full if d.get("auto") else _job_split, mode, opts)
                return self._send(200, {"ok": True})
            if path == "/api/alt":
                p = self._proj()
                mode = d["mode"]
                if p.meta.get("source") == "demo":
                    return self._send(400, {"error": "The demo song comes pre-split, so it can't be split with other models. Try this on one of your own songs."})
                if mode not in p.alt_modes():
                    if not SEP.demucs_available():
                        return self._send(409, {"error": "need_model", "plan": _model_plan()})
                    need = SEP.NEEDS_EXTRA.get(mode)
                    if need and not INST.extra_installed(need):
                        return self._send(409, {"error": "need_extra", "extras": [need]})
                S.job["result"] = None
                _start_job(_job_alt, mode, float(d["start"]), float(d["end"]), d.get("stem"),
                           min(max(float(d.get("blend") or 0), 0.0), 1.0))
                return self._send(200, {"ok": True})
            if path == "/api/detect":
                if not self._proj().instruments:
                    return self._send(400, {"error": "Split the song into instruments first."})
                _start_job(_job_detect)
                return self._send(200, {"ok": True})
            if path == "/api/vinyl":
                self._proj()
                st_ = d.get("strength", "normal")
                if st_ not in ("light", "normal", "strong"):
                    raise ValueError("Unknown strength.")
                _start_job(_job_vinyl, st_)
                return self._send(200, {"ok": True})
            if path == "/api/gpu":
                ok, why = INST.gpu_ready()
                DG.line(f"graphics card button: {'ready' if ok else why}")
                if not ok or sys.platform == "darwin":
                    return self._send(400 if not ok else 200, {"error": why} if not ok else {"ok": True, "message": why})
                card = INST.plan().get("gpu") or "NVIDIA card"
                if S.model.get("device_kind") == "cuda":
                    return self._send(200, {"ok": True, "message": f"Already using your graphics card: {S.model.get('device')}."})
                SEP._DEVICE = None
                start_model_install(force=True, gpu=True)   # swaps in the graphics-card build for this driver
                return self._send(200, {"ok": True, "message": f"Setting up the separation model for your {card}. It "
                                                             f"downloads once (about 3 GB); watch the model light."})
            if path == "/api/deep":
                if self._proj().stage != "detected":
                    raise ValueError("Find damage first (step 3).")
                _start_job(_job_deep)
                return self._send(200, {"ok": True})
            if path == "/api/vinyl/stems":
                if not self._proj().instruments:
                    raise ValueError("Split the song first.")
                st_ = d.get("strength", "normal")
                if st_ not in ("light", "normal", "strong"):
                    raise ValueError("Unknown strength.")
                _start_job(_job_declick_stems, st_)
                return self._send(200, {"ok": True})
            if path == "/api/vinyl/undo":
                self._proj()
                _start_job(_job_vinyl_undo)
                return self._send(200, {"ok": True})
            if path == "/api/auto/plan":
                return self._send(200, {"steps": self._proj().build_plan()})
            if path == "/api/auto/build":
                steps = [x for x in (d.get("steps") or []) if x in self._proj().AUTO_STEPS]
                if not steps:
                    return self._send(400, {"error": "Choose at least one step."})
                _start_job(_job_build, steps)
                return self._send(200, {"ok": True})
            if path == "/api/import_ref":
                kind = d.get("kind") if d.get("kind") in ("sample", "track", "auto") else "auto"
                items = d.get("tracks") or [{"path": d.get("path"), "stem": d.get("stem"), "name": d.get("name"),
                                             "use": d.get("use")}]
                todo = []
                for it in items[:12]:
                    src = os.path.realpath(str(it.get("path") or ""))
                    if not src.startswith(os.path.realpath(UPLOADS) + os.sep) or not os.path.isfile(src):
                        return self._send(400, {"error": "That file isn't available."})
                    stem = str(it.get("stem") or "")
                    if kind == "track" and stem not in self._proj().instruments and stem != "auto":
                        return self._send(400, {"error": f"Choose which instrument {it.get('name') or 'each file'} is."})
                    todo.append((src, stem, os.path.basename(str(it.get("name") or src))[:80],
                                 "all" if it.get("use") == "all" else "vocals"))
                if not todo:
                    return self._send(400, {"error": "Choose a file."})
                _start_job(_job_import_ref, todo, kind, bool(d.get("apply", True)))
                return self._send(200, {"ok": True})
            if path == "/api/open_stems":
                folder = d["folder"]
                _start_job(_job_stems, folder, d.get("name") or os.path.basename(folder.rstrip("/\\")), bool(d.get("auto")))
                return self._send(200, {"ok": True})
            if path == "/api/demo":
                _start_job(_job_demo)
                return self._send(200, {"ok": True})
            if path == "/api/load":
                S.project = _Project().load(os.path.join(PROJECTS, os.path.basename(str(d["id"]))))
                S.rev += 1
                S.audio_cache.clear()
                return self._send(200, {"ok": True})
            if path == "/api/delete_project":
                pid = os.path.basename(str(d.get("id") or ""))
                if pid in ("", ".", ".."):
                    raise ValueError("No project given.")
                shutil.rmtree(os.path.join(PROJECTS, pid), ignore_errors=True)
                return self._send(200, {"ok": True})
            if path == "/api/extra/install":
                start_extra_install(d["key"])
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
                prefs.update({k: bool(v) for k, v in d.items() if k in ("check_on_start", "auto_install", "roformer_passes")})
                UP.save_prefs(HOME, prefs)
                return self._send(200, {"ok": True, "prefs": prefs})
            if path == "/api/move_home":
                home = UP.move_home()
                return self._send(200, {"ok": True, "home": home})
            if path == "/api/stop":
                n = stop_everything()
                return self._send(200, {"ok": True, "stopped": n})
            if path == "/api/booted":
                try:
                    os.remove(MARKER)
                except OSError:
                    pass
                DG.mark(f"window ready (page took {d.get('ms', '?')} ms)")
                return self._send(200, {"ok": True})
            if path == "/api/reveal":           # works with no song open (Settings, start screen)
                reveal(d.get("path") or HOME)
                return self._send(200, {"ok": True})
            if path == "/api/clientlog":
                DG.line(f"window: {str(d.get('msg', ''))[:2000]}")
                return self._send(200, {"ok": True})
            if path == "/api/log/export":
                extra = {"App version": __version__, "Model": {k: v for k, v in S.model.items() if k != "plan"},
                         "Song open": S.project.meta.get("name") if S.project else None,
                         "Songs saved": len(list_projects()), "Safe mode": getattr(S, "safe", False)}
                path_ = DG.export_bundle(DG.log_dir(), extra)
                if d.get("reveal", True):
                    reveal(path_)
                return self._send(200, {"ok": True, "path": path_})
            if path == "/api/reset":
                what = d.get("what")
                if what == "models":
                    stop_everything()
                    n = clear_model_files()
                    return self._send(200, {"ok": True, "message": f"Removed {n} downloaded model file{'s' if n != 1 else ''}. They download again the next time you split a song."})
                if what == "settings":
                    try:
                        os.remove(UP.prefs_path(HOME))
                    except OSError:
                        pass
                    return self._send(200, {"ok": True, "message": "App settings are back to their defaults."})
                if what == "project":
                    S.project = None
                    S.rev += 1
                    return self._send(200, {"ok": True, "message": "Closed the open song. Your projects are untouched."})
                return self._send(400, {"error": "Unknown reset."})
            if path in ("/api/update/restart", "/api/restart"):
                if S.job["running"] and not d.get("force"):
                    return self._send(400, {"error": "Something is still running. Press Stop first, or restart anyway."})
                stop_everything()
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
            elif path == "/api/glitch_scan":
                p.scan_glitches(d.get("start"), d.get("end"))
            elif path == "/api/apply_best":
                ids = p.apply_best(d.get("kind", "all"), bool(d.get("accept", True)), bool(d.get("blend", False)))
                res = self._edited()
                res["new_ids"] = ids
                return self._send(200, res)
            elif path in ("/api/hits", "/api/bars", "/api/cleanup"):
                if path == "/api/hits":
                    ids = p.make_hits(d["stem"], int(d["sound"]), blend=bool(d.get("blend", False)))
                elif path == "/api/bars":
                    ids = [p.make_bars(d["stem"], int(d["best"]), [int(v) for v in d["damaged"]],
                                       [int(v) for v in d.get("members") or []], blend=bool(d.get("blend", False)))]
                else:
                    ids = p.clean_up(float(d["start"]), float(d["end"]))
                p.check_ids(ids)
                res = self._edited()
                res["new_ids"] = ids
                return self._send(200, res)
            elif path == "/api/pattern":
                ids = p.make_pattern_hits()
                res = self._edited()
                res["new_ids"] = ids
                return self._send(200, res)
            elif path == "/api/take":
                sid = p.make_take(d["stem"], int(d["section"]), int(d["source"]))
                p.check_ids([sid])
                res = self._edited()
                res["new_ids"] = [sid]
                return self._send(200, res)
            elif path == "/api/rebuild":
                ids = p.rebuild_range(float(d["start"]), float(d["end"]), d.get("stem") or None)
                if not ids:
                    return self._send(400, {"error": "Nothing in that part matches anywhere else in the song closely enough to rebuild from. Try a longer part, or another instrument."})
                p.check_ids(ids)
                res = self._edited()
                res["new_ids"] = ids
                return self._send(200, res)
            elif path == "/api/smooth":
                ids = p.smooth_range(float(d["start"]), float(d["end"]), d.get("stems"))
                res = self._edited()
                res["new_ids"] = ids
                return self._send(200, res)
            elif path == "/api/verify":
                flagged = p.verify_all()
                res = self._edited()
                res["flagged"] = flagged
                return self._send(200, res)
            elif path == "/api/best_of":
                ids = p.best_of_models()
                res = self._edited()
                res["new_ids"] = ids
                return self._send(200, res)
            elif path == "/api/quality":
                p.quality()
            elif path == "/api/ref_remove":
                p.remove_reference(str(d["stem"]))
                p.save()
            elif path == "/api/rename_section":
                p.rename_section(d["index"], d["name"])
            elif path == "/api/export":
                folder = os.path.join(EXPORTS, re.sub(r"[^\w\- ()]", "", p.meta.get("name", "song")) or "song")
                fmt = d.get("format", "wav")
                if fmt not in ("wav", "flac", "mp3"):
                    raise ValueError("Unknown format.")
                files = p.export(folder, fmt, tuple(d.get("what", ["mix", "stems", "vocals", "report"])),
                                 match_level=bool(d.get("match_level", False)) and not d.get("lufs"),
                                 declick=bool(d.get("declick", False)), lufs=-14.0 if d.get("lufs") else None)
                if d.get("reveal", True):
                    reveal(folder)
                return self._send(200, {"ok": True, "folder": folder, "files": files,
                                        "lufs": p.meta.get("last_export_lufs")})
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
    time.sleep(15)          # let the window settle first: setting up the secure connection is heavy
    do_check()
    info = S.update.get("info") or {}
    if info.get("available") and prefs.get("auto_install"):
        try:
            start_install(auto=True)
        except Exception as e:
            S.update["error"] = str(e)


def serve(port=0, just_updated=False, safe=False):
    for d in (PROJECTS, EXPORTS, UPLOADS):
        os.makedirs(d, exist_ok=True)
    S.just_updated = just_updated
    S.safe = safe
    if not safe:
        threading.Thread(target=_startup_update, daemon=True).start()
    # nothing is downloaded without asking: just find out what the download would be
    try:
        S.model["installed"] = SEP.demucs_available()
    except Exception:
        S.model["installed"] = False
    if safe:
        S.model["device"] = "not checked (safe mode)"
    elif S.model["installed"]:
        # loading PyTorch to look at the graphics card is heavy on the disk: let the window come up first
        S.model["device"] = S.model.get("device") or "checking..."
        t = threading.Timer(6.0, _probe_device)
        t.daemon = True
        t.start()
    else:
        threading.Thread(target=_model_plan, daemon=True).start()
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
