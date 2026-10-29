#!/bin/sh
# Mac: double-click to start. The app keeps running after this Terminal window closes.
cd "$(dirname "$0")" || exit 1
nohup python3 app.py >/dev/null 2>&1 &
(sleep 1; osascript -e 'tell application "Terminal" to close (every window whose name contains "Instrumental Studio")' >/dev/null 2>&1) &
exit 0
