#!/usr/bin/env bash
# Build a signed .app that runs Python, so a program keeps its macOS grants across Python upgrades.
#
#   build.sh <output .app path>      prints the path of the executable to run
#
# Run the program as `<executable> -m your.module` or `<executable> script.py`. Settings come from
# the environment:
#   CLAUDE_HUMAN_BUNDLE_ID  CFBundleIdentifier (required). Grants are held against it.
#   CLAUDE_HUMAN_APP_NAME   the name macOS shows in Privacy & Security (default: the .app's name).
#   CLAUDE_HUMAN_EXE        the executable's name (default: the app name in lowercase, dashes).
#   CLAUDE_HUMAN_PYTHON     the Python to run (default /opt/homebrew/bin/python3). Its framework is
#                           linked through the stable opt path, so an upgrade does not break it.
#   CLAUDE_HUMAN_IDENTITY   "auto" (default) uses the first "Apple Development" identity and falls
#                           back to ad-hoc; "-" signs ad-hoc; or an identity name.
#
# ⭐ TCC TRUSTS THE BINARY THAT RUNS, AND AN AD-HOC SIGNATURE TIES THE GRANT TO ITS EXACT BYTES.
# Homebrew signs Python.app ad hoc, so a Screen Recording or Accessibility grant given to it is
# held as `cdhash H"..."`, the hash of one build. On 2026-10-10 a dependency install updated
# python@3.14 and a program running Homebrew's Python.app lost both grants. `brew pin` does not
# prevent it: Homebrew upgrades a pinned formula another formula needs. Two shortcuts also failed:
#  * a bundle whose executable is a script ending in `exec python3 ...`: exec replaces the process
#    image, so the binary asking is python3 itself and the grant to the bundle does nothing;
#  * a bundle holding a copy of bin/python3.x: same bytes as the original, and its TCC verdict.
# So the executable is a copy of Python.app's small launcher (it only calls into the framework),
# re-pointed at the framework through the opt path and signed with a real certificate. The grant
# is then held against `identifier "<bundle id>" and certificate ...`, which a rebuild keeps.
# No hardened runtime: it would refuse to load Homebrew's unsigned libpython and extensions.
#
# ⭐ EACH COPY GETS ITS OWN MACH-O UUID, OR A NEW BUNDLE CUTS PYTHON OFF THE LOCAL NETWORK. macOS applies
# its Local Network rules by the UUID of the binary, and a plain copy keeps the UUID of Python.app. A
# bundle id nobody has answered for yet starts as denied, so building a new app denied that UUID for
# every program running the same launcher, including Python itself and other bundles already allowed:
# their connects to the LAN failed with EHOSTUNREACH ("No route to host"), with no prompt, while the
# internet and programs signed by Apple still worked. So the build writes a UUID of its own, made from
# the bundle id, before it signs.
#
# The bundle is rebuilt only when it is not current (missing, signature broken, made from another
# launcher, sharing the launcher's UUID, or it does not start Python), so running this at every
# install is cheap and safe.
set -euo pipefail
APP="${1:?usage: build.sh <output .app path>}"
BUNDLE_ID="${CLAUDE_HUMAN_BUNDLE_ID:-}"
case "$APP" in
  *.app) ;;
  *) echo "the output path must end in .app: $APP" >&2; exit 2 ;;
esac
case "$BUNDLE_ID" in
  *[!A-Za-z0-9.-]*|"") echo "invalid bundle id: '$BUNDLE_ID' (set CLAUDE_HUMAN_BUNDLE_ID)" >&2; exit 2 ;;
esac
NAME="${CLAUDE_HUMAN_APP_NAME:-$(basename "$APP" .app)}"
EXE_NAME="${CLAUDE_HUMAN_EXE:-$(printf '%s' "$NAME" | tr '[:upper:] ' '[:lower:]-')}"
case "$EXE_NAME" in
  *[!A-Za-z0-9._-]*|"") echo "invalid executable name: $EXE_NAME" >&2; exit 2 ;;
esac
IDENTITY="${CLAUDE_HUMAN_IDENTITY:-auto}"
PY="${CLAUDE_HUMAN_PYTHON:-/opt/homebrew/bin/python3}"
EXE="$APP/Contents/MacOS/$EXE_NAME"
PY_HOME="$("$PY" -c 'import sys;print(sys.base_prefix)')"   # the opt path, stable across upgrades
SRC="$PY_HOME/Resources/Python.app/Contents/MacOS/Python"
STAMP="$APP/Contents/Resources/source.sha256"
[ -x "$SRC" ] || { echo "no Python.app launcher at $SRC" >&2; exit 1; }

if [ "$IDENTITY" = auto ]; then
  IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
    | grep -o '"Apple Development: [^"]*"' | head -1 | tr -d '"' || true)"
  if [ -z "$IDENTITY" ]; then
    echo "warning: no Apple Development identity found, signing ad-hoc (a grant will not survive a rebuild)" >&2
    IDENTITY="-"
  fi
fi

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

macho_uuid() { otool -l "$1" 2>/dev/null | awk '$1 == "uuid" {print $2; exit}'; }

# Writes a UUID made from the bundle id into every architecture's LC_UUID. The signature goes first,
# since this changes the bytes it covers.
give_own_uuid() {
  "$PY" - "$1" "$BUNDLE_ID" <<'PYTHON'
import struct, sys, uuid
path, bundle_id = sys.argv[1:3]
data = bytearray(open(path, "rb").read())
FAT, THIN, LC_UUID = 0xcafebabe, 0xfeedfacf, 0x1b


def patch(start):
    magic = struct.unpack_from("<I", data, start)[0]
    if magic != THIN:
        raise SystemExit(f"not a 64-bit Mach-O at offset {start}: {magic:#x}")
    ncmds = struct.unpack_from("<I", data, start + 16)[0]
    at = start + 32
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, at)
        if cmd == LC_UUID:
            old = uuid.UUID(bytes=bytes(data[at + 8:at + 24]))
            data[at + 8:at + 24] = uuid.uuid5(old, bundle_id).bytes
            return
        at += size
    raise SystemExit("the launcher has no LC_UUID")


if struct.unpack_from(">I", data, 0)[0] == FAT:
    for i in range(struct.unpack_from(">I", data, 4)[0]):
        patch(struct.unpack_from(">I", data, 8 + i * 20 + 8)[0])
else:
    patch(0)
open(path, "wb").write(data)
PYTHON
}

current() {
  [ -x "$EXE" ] && codesign --verify --strict "$APP" >/dev/null 2>&1 \
    && [ "$(cat "$STAMP" 2>/dev/null)" = "$(shasum -a 256 "$SRC" | cut -d' ' -f1)" ] \
    && [ "$(macho_uuid "$EXE")" != "$(macho_uuid "$SRC")" ] \
    && /usr/bin/grep -q "<string>$BUNDLE_ID</string>" "$APP/Contents/Info.plist" \
    && "$EXE" -c 'import sys' >/dev/null 2>&1
}

if ! current; then
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  cp "$SRC" "$EXE"
  chmod u+w "$EXE"
  codesign --remove-signature "$EXE"
  # the launcher names the framework by its versioned Cellar folder, which Homebrew deletes at the
  # next upgrade; the opt path follows the upgrade instead
  old="$(otool -L "$EXE" | awk '/Python\.framework\/Versions\/[0-9.]+\/Python /{print $1; exit}')"
  [ -n "$old" ] || { echo "the launcher does not link a Python framework" >&2; exit 1; }
  install_name_tool -change "$old" "$PY_HOME/Python" "$EXE" 2>/dev/null
  give_own_uuid "$EXE"
  cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$(xml_escape "$NAME")</string>
  <key>CFBundleDisplayName</key><string>$(xml_escape "$NAME")</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>$EXE_NAME</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
  shasum -a 256 "$SRC" | cut -d' ' -f1 > "$STAMP"
  codesign --force --sign "$IDENTITY" "$APP" >&2
  current || { echo "the rebuilt bundle does not verify or does not start Python" >&2; exit 1; }
fi
echo "$EXE"
