#!/usr/bin/env bash
# Build sckshot.app, the ScreenCaptureKit screenshot tool.
#
#   build.sh <output .app path>
#
# Settings come from the environment:
#   CLAUDE_HUMAN_IDENTITY   code signing identity. "auto" (the default) uses the first
#                           "Apple Development" identity in the keychain and falls back to ad-hoc
#                           signing. "-" signs ad-hoc. "none" skips signing (for CI).
#   CLAUDE_HUMAN_BUNDLE_ID  CFBundleIdentifier (default local.claude-human.sckshot). Screen Recording
#                           is granted to this identifier, so keep it stable between builds.
#   CLAUDE_HUMAN_USAGE      the NSScreenCaptureUsageDescription text.
#
# It is an .app bundle and not a bare binary because macOS lists only app bundles under Screen
# Recording. An ad-hoc signature changes on every build, so a grant given to it does not survive a
# rebuild. Sign with a real identity if you want the grant to stay.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:?usage: build.sh <output .app path>}"
IDENTITY="${CLAUDE_HUMAN_IDENTITY:-auto}"
BUNDLE_ID="${CLAUDE_HUMAN_BUNDLE_ID:-local.claude-human.sckshot}"
USAGE="${CLAUDE_HUMAN_USAGE:-sckshot captures the screen or one window so it can be shown somewhere else.}"

case "$BUNDLE_ID" in
  *[!A-Za-z0-9.-]*|"") echo "invalid bundle id: $BUNDLE_ID" >&2; exit 2 ;;
esac
case "$OUT" in
  *.app) ;;
  *) echo "the output path must end in .app: $OUT" >&2; exit 2 ;;
esac

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/sckshot"
swiftc -O -parse-as-library -o "$BIN" "$HERE/sckshot.swift"

if [ "$IDENTITY" = auto ]; then
  IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
    | grep -o '"Apple Development: [^"]*"' | head -1 | tr -d '"' || true)"
  if [ -z "$IDENTITY" ]; then
    echo "warning: no Apple Development identity found, signing ad-hoc (a Screen Recording grant will not survive a rebuild)" >&2
    IDENTITY="-"
  fi
fi

sign() {
  case "$IDENTITY" in
    none) ;;
    -) codesign --force --sign - "$1" ;;
    *) codesign --force --options runtime --sign "$IDENTITY" "$1" ;;
  esac
}

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

sign "$BIN"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS"
cp "$BIN" "$OUT/Contents/MacOS/sckshot"
cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>sckshot</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleName</key><string>sckshot</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSScreenCaptureUsageDescription</key><string>$(xml_escape "$USAGE")</string>
</dict>
</plist>
PLIST
plutil -lint "$OUT/Contents/Info.plist" >/dev/null
sign "$OUT"
echo "built $OUT"
echo "grant it once in System Settings > Privacy & Security > Screen Recording > + > $OUT"
