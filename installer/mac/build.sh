#!/bin/bash
# Build "Instrumental Studio.app" and a drag-to-Applications .dmg on a Mac (used by .github/workflows/installers.yml),
# after Studio Lab's.
#   installer/mac/build.sh <python-build-standalone install_only tarball> <label, e.g. AppleSilicon> <version>
# Run from the repository root. Output: dist/InstrumentalStudio-<version>-Mac-<label>.dmg
set -euo pipefail
PYTAR="$1"; LABEL="$2"; VER="$3"
APP="build/mac/Instrumental Studio.app"
rm -rf build/mac && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app/bin" dist

# the program (exactly what's committed) and a private Python with the small packages; the separation model
# (PyTorch + Demucs) installs itself on first start
git archive HEAD | tar -x -C "$APP/Contents/Resources/app"
tar -xzf "$PYTAR" -C "$APP/Contents/Resources"                      # -> Resources/python
PY="$APP/Contents/Resources/python/bin/python3"
export PYTHONPYCACHEPREFIX="$PWD/build/pycache"
"$PY" -m pip install --disable-pip-version-check --no-warn-script-location -q numpy scipy pywebview imageio-ffmpeg
"$PY" -c "import numpy, scipy; print('bundled python ok')"
# ffmpeg (reads MP3, M4A, FLAC ...): the static build that comes with imageio-ffmpeg, as bin/ffmpeg next to the app
FF="$("$PY" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
cp "$FF" "$APP/Contents/Resources/app/bin/ffmpeg"
chmod +x "$APP/Contents/Resources/app/bin/ffmpeg"
"$APP/Contents/Resources/app/bin/ffmpeg" -hide_banner -version | head -1

cp installer/mac/launcher.sh "$APP/Contents/MacOS/Instrumental Studio"
chmod +x "$APP/Contents/MacOS/Instrumental Studio"

# icon
ICS="build/mac/icon.iconset"; mkdir -p "$ICS"
for s in 16 32 128 256 512; do
  sips -z $s $s installer/icon.png --out "$ICS/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) installer/icon.png --out "$ICS/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICS" -o "$APP/Contents/Resources/instrumental-studio.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Instrumental Studio</string>
  <key>CFBundleDisplayName</key><string>Instrumental Studio</string>
  <key>CFBundleIdentifier</key><string>io.github.kefirman9000.instrumentalstudio</string>
  <key>CFBundleVersion</key><string>${VER}</string>
  <key>CFBundleShortVersionString</key><string>${VER}</string>
  <key>CFBundleExecutable</key><string>Instrumental Studio</string>
  <key>CFBundleIconFile</key><string>instrumental-studio</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

# self-test: the bundled Python runs the engine test, and the bundled ffmpeg opens a compressed song
TESTHOME="$PWD/build/testhome"; rm -rf "$TESTHOME"; mkdir -p "$TESTHOME"
# (tests aren't shipped in the app: run the repository's engine test with the app's own Python)
INSTRUMENTAL_STUDIO_HOME="$TESTHOME" "$APP/Contents/Resources/python/bin/python3" tests/test_engine.py
"$APP/Contents/Resources/app/bin/ffmpeg" -hide_banner -loglevel error -f lavfi -i "sine=frequency=440:duration=2" -c:a libmp3lame "$TESTHOME/t.mp3" \
  || "$APP/Contents/Resources/app/bin/ffmpeg" -hide_banner -loglevel error -f lavfi -i "sine=frequency=440:duration=2" "$TESTHOME/t.m4a"
T="$(ls "$TESTHOME"/t.* | head -1)"
(cd "$APP/Contents/Resources/app" && PATH="$PWD/bin:$PATH" "$PWD/../python/bin/python3" -c "
import sys; sys.path.insert(0, '.')
from studio.audio import load_audio
x, sr = load_audio(sys.argv[1])[:2]
assert x.shape[1] > sr, x.shape
print('bundled ffmpeg opens songs ok')" "$T")
echo "app self-test ok"

# ad-hoc signature (no Apple developer ID): required for the bundled Python to run on Apple Silicon
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
codesign --force --deep --sign - "$APP"
codesign --verify --deep "$APP"

# disk image: the app plus an Applications shortcut to drag it onto
STAGE="build/mac/dmg"; rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
for try in 1 2 3 4 5; do
  if hdiutil create -volname "Instrumental Studio" -srcfolder "$STAGE" -ov -format UDZO "dist/InstrumentalStudio-${VER}-Mac-${LABEL}.dmg"; then
    break
  fi
  [ "$try" = 5 ] && exit 1
  echo "hdiutil busy - retrying in $((try * 10))s"; sleep $((try * 10))
done
echo "built dist/InstrumentalStudio-${VER}-Mac-${LABEL}.dmg"
