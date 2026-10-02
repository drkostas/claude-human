#!/usr/bin/env bash
# Build vhid_type, the virtual HID keyboard helper.
#
#   build.sh <output path>
#
# vhid_type talks to the running Karabiner-VirtualHIDDevice-Daemon, so it must be compiled against
# the pqrs client library at the same protocol version as the installed daemon. The default commit
# is the one Karabiner-Elements 15.4.0 bundles (daemon 6.0.0, client protocol version 5). After a
# Karabiner-Elements upgrade, set CLAUDE_HUMAN_PQRS_COMMIT to the
# Karabiner-DriverKit-VirtualHIDDevice submodule commit of the new release and build again.
#
# CLAUDE_HUMAN_PQRS_SRC can point at an existing checkout of that repository instead of cloning.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:?usage: build.sh <output path>}"
PQRS_COMMIT="${CLAUDE_HUMAN_PQRS_COMMIT:-3daa7993af567ac0e494fe0aa9d3feee9035d5bf}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SRC="${CLAUDE_HUMAN_PQRS_SRC:-}"
if [ -z "$SRC" ]; then
  SRC="$WORK/src"
  git clone --quiet --filter=blob:none \
    https://github.com/pqrs-org/Karabiner-DriverKit-VirtualHIDDevice.git "$SRC"
  git -C "$SRC" checkout --quiet "$PQRS_COMMIT"
fi

mkdir -p "$(dirname "$OUT")"
clang++ -std=gnu++20 -O2 -w \
  -I "$SRC/vendor/vendor/include" \
  -I "$SRC/include" \
  "$HERE/type_string.cpp" -o "$OUT" \
  -framework CoreFoundation -framework IOKit -framework Foundation -lpthread
echo "built $OUT"
