#!/bin/bash
# Instrumental Studio: installs everything it needs on a Mac that has nothing yet.
#   1. Python 3.12 from python.org (if no Python 3.10-3.13 is found), with its web certificates
#   2. ffmpeg (reads MP3, M4A, FLAC...): with Homebrew if you have it, otherwise a ready-made copy
#   3. The app's Python packages
#   4. Offers the separation model download at the end
# Safe to run again: it skips anything that's already there. Double-click it in Finder.
cd "$(dirname "$0")" || exit 1
PYVER="3.12.8"
echo
echo " Instrumental Studio setup"
echo " ========================="
echo " This installs everything the app needs. It takes about 10 minutes, mostly downloads."
echo " Your Mac password may be asked for once (to install Python and ffmpeg)."
echo

find_python() {
    PY=""
    for c in python3.12 python3.13 python3.11 python3.10 \
             /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
             /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
        p=$(command -v "$c" 2>/dev/null) || continue
        # /usr/bin/python3 on a fresh Mac is a stub that pops up a developer-tools install: skip it
        [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1 && continue
        if "$p" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
            PY="$p"; return 0
        fi
    done
    return 1
}

pause_exit() { printf '\nPress Return to close this window.'; read -r _; exit "${1:-0}"; }

# ---------------------------------------------------------------- 1. Python
if ! find_python; then
    echo "[1/4] Installing Python $PYVER from python.org ..."
    PKG="/tmp/python-$PYVER-macos11.pkg"
    if ! curl -fL --progress-bar -o "$PKG" "https://www.python.org/ftp/python/$PYVER/python-$PYVER-macos11.pkg"; then
        echo
        echo " Couldn't download Python. Check your internet connection, or install Python 3.12 yourself from"
        echo " https://www.python.org/downloads/ and run this again."
        pause_exit 1
    fi
    sudo installer -pkg "$PKG" -target / || { echo " Python's installer didn't finish."; pause_exit 1; }
    rm -f "$PKG"
    CERTS="/Applications/Python 3.12/Install Certificates.command"
    [ -f "$CERTS" ] && bash "$CERTS" >/dev/null 2>&1
    find_python || { echo " Python was installed but can't be found. Close this window and run this file again."; pause_exit 1; }
fi
echo "[1/4] Python: $PY"
echo

# ---------------------------------------------------------------- 2. ffmpeg
echo "[2/4] ffmpeg ..."
if command -v ffmpeg >/dev/null 2>&1 || [ -x /usr/local/bin/ffmpeg ]; then
    echo "       already installed"
elif command -v brew >/dev/null 2>&1; then
    brew install ffmpeg
else
    # no Homebrew: a ready-made ffmpeg (evermeet.cx, Intel; Apple silicon Macs run it with Rosetta)
    if [ "$(uname -m)" = "arm64" ] && ! /usr/bin/pgrep -q oahd; then
        echo "       Setting up Rosetta (lets this Mac run the ready-made ffmpeg) ..."
        sudo softwareupdate --install-rosetta --agree-to-license >/dev/null 2>&1
    fi
    TMPD=$(mktemp -d)
    if curl -fL --progress-bar -o "$TMPD/ffmpeg.zip" "https://evermeet.cx/ffmpeg/getrelease/zip" \
       && unzip -q -o "$TMPD/ffmpeg.zip" -d "$TMPD" && [ -f "$TMPD/ffmpeg" ]; then
        sudo mkdir -p /usr/local/bin && sudo mv "$TMPD/ffmpeg" /usr/local/bin/ffmpeg && sudo chmod +x /usr/local/bin/ffmpeg
        sudo xattr -d com.apple.quarantine /usr/local/bin/ffmpeg 2>/dev/null
        echo "       installed to /usr/local/bin/ffmpeg"
    else
        echo "       Couldn't download ffmpeg. Without it only WAV songs open; you can install it later"
        echo "       with Homebrew (https://brew.sh, then: brew install ffmpeg)."
    fi
    rm -rf "$TMPD"
fi
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
echo

# ---------------------------------------------------------------- 3. packages
echo "[3/4] The app's packages ..."
"$PY" -m ensurepip --upgrade >/dev/null 2>&1
"$PY" -m pip install --upgrade pip >/dev/null 2>&1
"$PY" -m pip install -r requirements.txt
echo

# ---------------------------------------------------------------- 4. check + model
echo "[4/4] Checking everything (and offering the separation model download) ..."
echo
"$PY" check_setup.py
echo
echo " When everything shows OK, start the app with \"Start Instrumental Studio (Mac).command\"."
pause_exit 0
