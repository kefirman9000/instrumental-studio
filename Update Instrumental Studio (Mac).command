#!/bin/sh
# Mac: double-click to update Instrumental Studio without opening it.
cd "$(dirname "$0")" || exit 1
python3 update.py; printf '\nPress Return to close this window.'; read _; exit 0
