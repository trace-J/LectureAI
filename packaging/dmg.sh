#!/bin/zsh
# Wrap a built Syllabus.app in a disk image people can drag to Applications.
#
#     packaging/dmg.sh [path/to/Syllabus.app] [output-dir]
#
# Defaults to dist/Syllabus.app and dist/. Writes Syllabus-<version>.dmg and
# a .sha256 beside it. Only hdiutil, which ships with macOS, is used: the
# image holds the app and an alias to /Applications, the plain Mac install.
set -euo pipefail

ROOT="${0:A:h:h}"
APP="${1:-$ROOT/dist/Syllabus.app}"
OUT="${2:-${APP:h}}"
APP="${APP:A}"
OUT="${OUT:A}"

[[ -d "$APP" ]] || { echo "dmg.sh: no app at $APP; run packaging/build.sh first" >&2; exit 1; }
VERSION="$(plutil -extract CFBundleShortVersionString raw -o - "$APP/Contents/Info.plist")"
NAME="Syllabus-${VERSION}"
DMG="$OUT/$NAME.dmg"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
# ditto keeps the bundle's symlinks and code signature intact; cp -R does too
# on modern macOS, but ditto is what Apple's own tooling uses.
ditto "$APP" "$STAGE/Syllabus.app"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG"
hdiutil create -quiet -volname "Syllabus" -srcfolder "$STAGE" -ov -format UDZO \
    -imagekey zlib-level=9 "$DMG"
(cd "$OUT" && shasum -a 256 "$NAME.dmg" > "$NAME.dmg.sha256")

echo "built $DMG ($(du -h "$DMG" | cut -f1))"
cat "$OUT/$NAME.dmg.sha256"
