#!/bin/zsh
# Build Syllabus.app from this checkout into dist/Syllabus.app.
#
#     packaging/build.sh
#
# Needs the checkout's Python with the app and build extras installed:
#     .venv/bin/pip install -e '.[app,build]'
# Set PYTHON to use another interpreter. Everything else it uses (sips,
# iconutil, codesign) ships with macOS.
#
# The result is signed ad hoc unless SYLLABUS_CODESIGN_IDENTITY names a
# Developer ID identity in the keychain (and SYLLABUS_ENTITLEMENTS a file).
set -euo pipefail

ROOT="${0:A:h:h}"
cd "$ROOT"
PY="${PYTHON:-$ROOT/.venv/bin/python}"

if ! "$PY" -c "import PyInstaller, webview" 2>/dev/null; then
    echo "build.sh: $PY lacks PyInstaller or pywebview." >&2
    echo "  install them:  $PY -m pip install -e '.[app,build]'" >&2
    exit 1
fi

# The app icon, from the panel's own: an iconset of the sizes macOS wants,
# folded into one .icns. 512 is the source size, so nothing is scaled up.
BUILD="$ROOT/packaging/build"
ICONSET="$BUILD/Syllabus.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for size in 16 32 128 256; do
    sips -z $size $size intake/static/icon.png \
        --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z $double $double intake/static/icon.png \
        --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
cp intake/static/icon.png "$ICONSET/icon_512x512.png"
iconutil -c icns "$ICONSET" -o "$BUILD/Syllabus.icns"

"$PY" -m PyInstaller --noconfirm --clean \
    --distpath "$ROOT/dist" --workpath "$BUILD/pyinstaller" \
    packaging/syllabus.spec

APP="$ROOT/dist/Syllabus.app"
codesign --verify --deep --strict "$APP"
echo
echo "built $APP ($(du -sh "$APP" | cut -f1))"
echo "  version $("$PY" -c 'import intake; print(intake.__version__)'), $(codesign -dv "$APP" 2>&1 | grep -o 'Signature=.*' || echo 'signed ad hoc')"
echo "  open it:  open '$APP'"
