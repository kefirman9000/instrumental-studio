#!/bin/sh
# Mac: double-click to free disk space by clearing split instruments from your songs.
cd "$(dirname "$0")" || exit 1
python3 clear_stems.py; printf '\nPress Return to close this window.'; read _; exit 0
