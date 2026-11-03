#!/bin/bash
# Instrumental Studio on Linux: installs Python, pip, ffmpeg and the app's packages, then runs the setup check.
#   ./install_everything.sh
# Uses your package manager (apt, dnf, pacman or zypper); asks for your password once.
cd "$(dirname "$0")" || exit 1
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv ffmpeg
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip ffmpeg
elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --needed --noconfirm python python-pip ffmpeg
elif command -v zypper >/dev/null 2>&1; then
    sudo zypper install -y python3 python3-pip ffmpeg
else
    echo "No supported package manager found: install Python 3.10-3.13, pip and ffmpeg yourself, then run this again."
fi
# newer distributions protect the system Python; the app's few packages are fine to add for this user
python3 -m pip install --user -r requirements.txt 2>/dev/null \
    || python3 -m pip install --user --break-system-packages -r requirements.txt
python3 check_setup.py
