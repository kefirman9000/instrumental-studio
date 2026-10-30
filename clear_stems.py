#!/usr/bin/env python3
"""Free disk space by clearing split instruments (stems) from your songs.

    python clear_stems.py           list songs and choose what to clear (asks before deleting)
    python clear_stems.py --list    only show how much space each song's split uses

What you can clear:
  1. Other models' splits only (from "Try another model here"). Your songs stay exactly as they are,
     including takes you already applied; the other model just splits again if you try it once more.
  2. A song's whole split. The song stays in your Songs list with its original recording and goes back
     to step 1 (Import): split it again whenever you like. Its repairs and level changes are cleared.

The original recordings are never deleted, nor are your exports. Close Instrumental Studio first.
Only uses Python's own library.
"""
import json
import os
import shutil
import sys
import zipfile

HOME = os.environ.get("INSTRUMENTAL_STUDIO_HOME", os.path.join(os.path.expanduser("~"), "InstrumentalStudio"))
PROJECTS = os.path.join(HOME, "projects")
FRESH_MIXER = {"stem_gain": {}, "mute": {}, "solo": {}, "section_gain": {}, "keep": [], "vocal_lift": {},
               "mute_ranges": [], "sides": [], "mud_cut": {}}


def size_of(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def gb(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e8 else f"{n / 1e6:.0f} MB"


def name_of(folder):
    for fn in ("summary.json",):
        try:
            with open(os.path.join(folder, fn)) as f:
                return json.load(f).get("name") or os.path.basename(folder)
        except (OSError, ValueError):
            pass
    return os.path.basename(folder)


def songs():
    out = []
    if not os.path.isdir(PROJECTS):
        return out
    for d in sorted(os.listdir(PROJECTS)):
        root = os.path.join(PROJECTS, d)
        if not os.path.isfile(os.path.join(root, "project.json")):
            continue
        sd = os.path.join(root, "stems")
        stems = [os.path.join(sd, f) for f in (os.listdir(sd) if os.path.isdir(sd) else [])
                 if f.endswith(".npy") and not f.startswith("_")]
        split = sum(size_of(p) for p in stems) + sum(size_of(os.path.join(root, f)) for f in ("patches.npz", "features.npz"))
        alt = size_of(os.path.join(root, "alt")) if os.path.isdir(os.path.join(root, "alt")) else 0
        out.append({"id": d, "root": root, "name": name_of(root), "stems": stems, "split": split, "alt": alt})
    return out


def show(lst):
    print(f"\nSongs in {PROJECTS}:\n")
    print(f"  {'#':>3}  {'split':>9}  {'other models':>12}  song")
    for i, s in enumerate(lst, 1):
        print(f"  {i:>3}  {gb(s['split']) if s['stems'] else '-':>9}  {gb(s['alt']) if s['alt'] else '-':>12}  {s['name']}")
    tot_split = sum(s["split"] for s in lst if s["stems"])
    tot_alt = sum(s["alt"] for s in lst)
    print(f"\n  Total: {gb(tot_split)} in splits, {gb(tot_alt)} in other models' splits.")


def clear_alt(s):
    p = os.path.join(s["root"], "alt")
    freed = s["alt"]
    shutil.rmtree(p, ignore_errors=True)
    return freed if not os.path.exists(p) else 0


def clear_split(s):
    """Back to step 1 (Import): keep the original recording and the song's name, drop everything made
    from the split. The app opens it like a freshly opened song."""
    root = s["root"]
    pj = os.path.join(root, "project.json")
    with open(pj, encoding="utf-8") as f:
        state = json.load(f)
    if not os.path.isfile(os.path.join(root, "stems", "_original.npy")):
        print(f"  Skipped {s['name']}: its original recording isn't there, so clearing would leave nothing to open.")
        return 0
    meta = state.get("meta", {})
    for k in ("stems", "sep_mode", "separation", "analyzed", "quality", "quality_before", "verified", "grid_drift",
              "glitch_scan_applied", "last_export_gain_db", "source_note"):
        meta.pop(k, None)
    meta["stage"] = "imported"
    meta["stems"] = []
    state.update(meta=meta, sections=[], beats=[], windows=[], VA=[], suggestions={}, mixer=dict(FRESH_MIXER))
    freed = 0
    for p in s["stems"]:
        try:
            freed += os.path.getsize(p)
            os.remove(p)
        except OSError as e:
            print(f"  Couldn't remove {os.path.basename(p)} ({e}). Is the app still open?")
            return freed
    for fn in ("features.npz",):
        p = os.path.join(root, fn)
        if os.path.exists(p):
            freed += os.path.getsize(p)
            os.remove(p)
    p = os.path.join(root, "patches.npz")
    if os.path.exists(p):
        freed += os.path.getsize(p)
    zipfile.ZipFile(p, "w").close()              # the app expects the file; an empty one means no repairs
    freed += clear_alt(s)
    tmp = pj + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, pj)
    with open(os.path.join(root, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"name": meta.get("name"), "duration": meta.get("duration")}, f)
    return freed


def pick(lst, prompt):
    a = input(prompt).strip().lower()
    if a in ("all", "a"):
        return lst
    chosen = []
    for part in a.replace(",", " ").split():
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.isdigit() and hi.isdigit():
                chosen += [lst[i - 1] for i in range(int(lo), int(hi) + 1) if 1 <= i <= len(lst)]
        elif part.isdigit() and 1 <= int(part) <= len(lst):
            chosen.append(lst[int(part) - 1])
    seen, out = set(), []
    for s in chosen:
        if s["id"] not in seen:
            seen.add(s["id"])
            out.append(s)
    return out


def main():
    lst = songs()
    if not lst:
        print(f"No songs found in {PROJECTS}.")
        return 0
    show(lst)
    if "--list" in sys.argv[1:]:
        return 0
    print("\nClose Instrumental Studio before clearing anything.\n")
    print("  1  Clear other models' splits only (safe: your songs stay exactly as they are)")
    print("  2  Clear songs' whole splits (songs go back to step 1; re-split any time)")
    print("  q  Quit without changing anything")
    try:
        c = input("\nChoose 1, 2 or q: ").strip().lower()
        if c not in ("1", "2"):
            print("Nothing was changed.")
            return 0
        pool = [s for s in lst if (s["alt"] if c == "1" else s["stems"])]
        if not pool:
            print("Nothing to clear there.")
            return 0
        if len(pool) != len(lst):
            show(pool)
        chosen = pick(pool, "\nWhich songs? Numbers from the list (e.g. 1 3 5-7), or 'all': ")
        if not chosen:
            print("Nothing was changed.")
            return 0
        what = "other models' splits" if c == "1" else "whole splits (repairs and level changes too)"
        size = sum(s["alt"] if c == "1" else s["split"] + s["alt"] for s in chosen)
        print(f"\nThis clears the {what} of {len(chosen)} song(s), freeing about {gb(size)}:")
        for s in chosen:
            print(f"  - {s['name']}")
        if input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was changed.")
            return 0
    except EOFError:
        print("Nothing was changed.")
        return 0
    freed = 0
    for s in chosen:
        try:
            freed += clear_alt(s) if c == "1" else clear_split(s)
            print(f"  Cleared {s['name']}")
        except Exception as e:
            print(f"  Couldn't clear {s['name']}: {e}")
    print(f"\nDone. Freed about {gb(freed)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
