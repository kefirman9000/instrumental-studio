#!/usr/bin/env python3
"""Update Instrumental Studio without opening it.

    python update.py          check, and install the newest version if there is one
    python update.py --check  only say whether an update is available

Uses the same updater as the app: the download is checked against the checksum published with the
release before anything is changed, the current version is backed up to .backup/, and it's put back
if installing fails. Your songs, projects and exports are never touched.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from studio import __version__  # noqa: E402
from studio import updater as UP  # noqa: E402


def main():
    only_check = "--check" in sys.argv[1:]
    print(f"Instrumental Studio {__version__}")
    print("Checking for updates...")
    try:
        info = UP.check()
    except Exception as e:
        print(f"\nCouldn't check for updates: {e}")
        return 1
    if not info.get("available"):
        print(f"You have the newest version ({info.get('current', __version__)}).")
        return 0
    print(f"\nVersion {info['latest']} is available (you have {info['current']}).")
    notes = (info.get("notes") or "").strip()
    if notes:
        print("\nWhat's new:\n" + "\n".join("  " + l for l in notes.splitlines()[:40]))
    if only_check:
        return 0
    print("\nClose Instrumental Studio before continuing, if it's open.")
    try:
        ok = input("Install it now? [Y/n] ").strip().lower() in ("", "y", "yes")
    except EOFError:
        ok = True                       # not interactive: go ahead
    if not ok:
        print("Nothing was changed.")
        return 0

    def progress(f, msg):
        print(f"  {int(round((f or 0) * 100)):3d}%  {msg}")

    try:
        v = UP.install(info, progress)
    except Exception as e:
        print(f"\nThe update didn't install: {e}")
        print("Your current version is unchanged.")
        return 1
    print(f"\nInstalled version {v}. Start Instrumental Studio as usual.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
