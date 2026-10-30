"""Check GitHub for a newer release and install it in place.

Releases are read from https://github.com/<REPO>/releases. Installing downloads the release's
source zip, keeps a backup of the current version, copies the new files over the app folder
(your projects and exports live elsewhere and are never touched), installs any new Python
requirements, and asks the app to restart. A git checkout is updated with `git pull` instead.
"""
import json
import os
import re
import shutil
import ssl
import subprocess

from . import proc  # noqa: E402  (hidden windows on Windows)
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

from . import __version__

REPO = os.environ.get("INSTRUMENTAL_STUDIO_REPO", "kefirman9000/instrumental-studio")
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEP = {".git", ".backup", "__pycache__"}
UA = {"User-Agent": f"InstrumentalStudio/{__version__}", "Accept": "application/vnd.github+json"}


def _ver(v):
    nums = [int(x) for x in re.findall(r"\d+", v or "0")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def _open(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            raise RuntimeError(
                "Your Python can't verify GitHub's certificate. On a Mac, run \"Install Certificates.command\" "
                "from your Python folder in Applications, then try again.") from e
        raise RuntimeError(f"Couldn't reach GitHub ({getattr(e, 'reason', e)}). Check your internet connection.") from e


def check():
    """Return info about the newest release and whether it is newer than this copy."""
    try:
        with _open(f"https://api.github.com/repos/{REPO}/releases/latest") as r:
            rel = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:   # no releases yet: the version on the main branch is the latest
            return _check_branch()
        if e.code == 403:
            raise RuntimeError("GitHub is limiting update checks right now. Try again in an hour.") from e
        raise RuntimeError(f"GitHub answered {e.code} while checking for updates.") from e
    latest = rel.get("tag_name", "").lstrip("vV")
    assets = rel.get("assets", []) or []
    zip_asset = next((a for a in assets if a.get("name", "").endswith(".zip")), None)
    sums = next((a for a in assets if a.get("name", "") in ("SHA256SUMS", "SHA256SUMS.txt")), None)
    digest = (zip_asset or {}).get("digest") or ""          # GitHub's own sha256 of the uploaded file
    return {
        "zip_name": (zip_asset or {}).get("name"),
        "sha256": digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else None,
        "sha256_url": (sums or {}).get("browser_download_url"),
        "verify": zip_asset is not None,      # a release download must match its published checksum
        "current": __version__, "latest": latest, "available": _ver(latest) > _ver(__version__),
        "name": rel.get("name") or f"Version {latest}", "notes": (rel.get("body") or "").strip(),
        "published": rel.get("published_at"), "url": rel.get("html_url"),
        # the ready-made download attached to the release, else GitHub's source zip
        "zip": next((a["browser_download_url"] for a in rel.get("assets", []) if a.get("name", "").endswith(".zip")),
                    rel.get("zipball_url")),
        "checked": time.time(),
    }


def _check_branch(branch="main"):
    """Fallback when no GitHub release exists: read __version__ from the branch and use its zip."""
    import base64
    try:
        with _open(f"https://api.github.com/repos/{REPO}/contents/studio/__init__.py?ref={branch}") as r:
            src = base64.b64decode(json.load(r)["content"]).decode()
        with _open(f"https://api.github.com/repos/{REPO}/commits/{branch}") as r:
            commit = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub answered {e.code} while checking for updates.") from e
    m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", src)
    latest = m.group(1) if m else "0"
    c = commit.get("commit", {})
    return {
        "current": __version__, "latest": latest, "available": _ver(latest) > _ver(__version__),
        "name": f"Version {latest}",
        "notes": "\n".join(l for l in (c.get("message") or "").splitlines()
                           if not re.match(r"(Co-Authored-By|Claude-Session|Signed-off-by):", l)).strip(),
        "published": (c.get("committer") or {}).get("date"),
        "url": f"https://github.com/{REPO}/commits/{branch}",
        "zip": f"https://api.github.com/repos/{REPO}/zipball/{branch}", "checked": time.time(),
    }


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify(zpath, info):
    """Refuse a release download whose checksum doesn't match the one published with the release
    (SHA256SUMS next to the zip, and GitHub's own record of the file). Nothing is changed on refusal."""
    expected = set()
    if info.get("sha256"):
        expected.add(info["sha256"])
    if info.get("sha256_url"):
        with _open(info["sha256_url"], timeout=30) as r:
            for line in r.read().decode("utf-8", "replace").splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[-1].lstrip("*") == info.get("zip_name"):
                    expected.add(parts[0].lower())
    if not expected:
        raise RuntimeError("This update has no published checksum, so it can't be verified; nothing was changed. "
                           "Download it by hand from the releases page if you trust it.")
    if len(expected) > 1:
        raise RuntimeError("The update's published checksums disagree; nothing was changed.")
    got = _sha256(zpath)
    if got != next(iter(expected)):
        raise RuntimeError("The download doesn't match its published checksum (it may have been damaged or "
                           "tampered with); nothing was changed. Try again later.")


def _is_git():
    return os.path.isdir(os.path.join(APP_DIR, ".git")) and shutil.which("git")


def install(info, progress=lambda f, m: None):
    """Install the release described by check(). Returns the installed version."""
    if not info.get("available"):
        raise RuntimeError("Already up to date.")
    old_req = _read(os.path.join(APP_DIR, "requirements.txt"))
    if _is_git():
        progress(0.2, "Pulling the new version")
        r = proc.run(["git", "-C", APP_DIR, "pull", "--ff-only"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("git pull failed: " + (r.stderr or r.stdout)[-300:])
    else:
        progress(0.1, "Downloading the update")
        with tempfile.TemporaryDirectory() as td:
            zpath = os.path.join(td, "update.zip")
            with _open(info["zip"], timeout=120) as r, open(zpath, "wb") as f:
                shutil.copyfileobj(r, f)
            if info.get("verify"):
                progress(0.4, "Checking the download")
                _verify(zpath, info)
            progress(0.5, "Unpacking")
            with zipfile.ZipFile(zpath) as z:
                for m in z.namelist():   # refuse paths that try to escape the folder
                    if m.startswith("/") or ".." in m.split("/"):
                        raise RuntimeError("The update archive looks malformed; nothing was changed.")
                z.extractall(os.path.join(td, "x"))
                for zi in z.infolist():   # zipfile drops permissions; keep launchers executable
                    mode = (zi.external_attr >> 16) & 0o777
                    if mode and not zi.is_dir():
                        os.chmod(os.path.join(td, "x", zi.filename), mode)
            tops = os.listdir(os.path.join(td, "x"))
            src = os.path.join(td, "x", tops[0]) if len(tops) == 1 else os.path.join(td, "x")
            if not os.path.exists(os.path.join(src, "app.py")):
                raise RuntimeError("The download doesn't look like Instrumental Studio; nothing was changed.")
            progress(0.65, "Backing up the current version")
            backup = os.path.join(APP_DIR, ".backup", __version__)
            shutil.rmtree(backup, ignore_errors=True)
            os.makedirs(backup, exist_ok=True)
            for name in os.listdir(APP_DIR):
                if name not in KEEP:
                    p = os.path.join(APP_DIR, name)
                    (shutil.copytree if os.path.isdir(p) else shutil.copy2)(p, os.path.join(backup, name))
            progress(0.75, "Installing")
            try:
                _copy_over(src, APP_DIR)
            except Exception:
                _copy_over(backup, APP_DIR)   # put the old version back
                raise
    new_req = _read(os.path.join(APP_DIR, "requirements.txt"))
    if new_req and new_req != old_req:
        progress(0.85, "Installing new requirements")
        proc.run([sys.executable, "-m", "pip", "install", "-r", os.path.join(APP_DIR, "requirements.txt")],
                       capture_output=True)
    progress(1.0, "Update installed")
    return info["latest"]


def _copy_over(src, dst):
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in KEEP]
        rel = os.path.relpath(root, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for f in files:
            # plain copy (fresh timestamps) so Python never reuses compiled files from the old version
            shutil.copy(os.path.join(root, f), os.path.join(dst, rel, f))
    for root, dirs, _ in os.walk(dst):
        if "__pycache__" in dirs:
            shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
            dirs.remove("__pycache__")


def _read(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return ""


# --------------------------------------------------------------------------- preferences + restart
def prefs_path(home):
    return os.path.join(home, "settings.json")


def load_prefs(home):
    try:
        with open(prefs_path(home)) as f:
            return {"check_on_start": True, "auto_install": False, "roformer_passes": False, **json.load(f)}
    except (OSError, ValueError):
        return {"check_on_start": True, "auto_install": False, "roformer_passes": False}


def save_prefs(home, prefs):
    os.makedirs(home, exist_ok=True)
    with open(prefs_path(home), "w") as f:
        json.dump(prefs, f, indent=2)


RESTART = threading.Event()


def relaunch():
    """Start a fresh copy of the app, then let this one exit."""
    args = [proc.gui_python(), os.path.join(APP_DIR, "app.py")] + [a for a in sys.argv[1:] if a != "--after-update"]
    args.append("--after-update")
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, cwd=APP_DIR, **kw)
    RESTART.set()
