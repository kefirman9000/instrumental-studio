#!/bin/bash
# Instrumental Studio.app/Contents/MacOS/Instrumental Studio  (after Studio Lab's launcher)
# The app bundle carries a private Python (Resources/python) and the program (Resources/app). The program is copied to
# ~/Library/Application Support/Instrumental Studio and run from there, with its own Python environment there too, so
# in-app updates and the separation model (installed on first start) live outside the signed, read-only bundle and
# survive dragging a newer Instrumental Studio into Applications. Your songs stay in ~/InstrumentalStudio.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
BASE="$RES/python/bin/python3"
DATA="$HOME/Library/Application Support/Instrumental Studio"
export PYTHONPYCACHEPREFIX="$HOME/Library/Caches/Instrumental Studio/pycache"   # never write inside the bundle
mkdir -p "$DATA"

# a Python environment of our own that sees the bundled packages (numpy, scipy, pywebview) and takes the model
VENV="$DATA/python"
if [ ! -x "$VENV/bin/python3" ] || ! "$VENV/bin/python3" -c "import numpy" 2>/dev/null; then
  rm -rf "$VENV"
  "$BASE" -m venv --system-site-packages "$VENV"
fi
PY="$VENV/bin/python3"

ver() { "$PY" -c "import re,sys;m=re.search(r'__version__\s*=\s*[\"\x27]([^\"\x27]+)',open(sys.argv[1]).read());print(m.group(1) if m else '0')" "$1" 2>/dev/null || echo 0; }
NEW="$(ver "$RES/app/studio/__init__.py")"
OLD="$(ver "$DATA/app/studio/__init__.py")"
if [ ! -f "$DATA/app/app.py" ] || "$PY" -c '
import sys
v = lambda s: tuple(int(x) for x in s.strip().split(".") if x.isdigit())
sys.exit(0 if v(sys.argv[1]) > v(sys.argv[2]) else 1)' "$NEW" "$OLD"; then
  # a newer program than the one in Application Support: copy it over (songs and settings are elsewhere)
  mkdir -p "$DATA/app"
  (cd "$RES/app" && tar -cf - .) | (cd "$DATA/app" && tar -xf -)
fi

cd "$DATA/app"
exec "$PY" "$DATA/app/app.py" "$@"
