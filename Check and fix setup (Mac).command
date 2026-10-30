#!/bin/sh
# Mac: double-click to check everything Instrumental Studio needs and install what's missing.
cd "$(dirname "$0")" || exit 1
python3 check_setup.py; printf '\nPress Return to close this window.'; read _; exit 0
