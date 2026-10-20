#!/usr/bin/env bash
# Compile the Kotlin the config plugin writes, against the Android platform only (it needs no
# library). The tests read the Kotlin as text, which shows what it says but not that it compiles.
#
#   scripts/compile.sh            render the job with the default options and compile it
#   scripts/compile.sh FILE.kt    compile a file a prebuild wrote
#
# Needs Java 17 or later, curl, unzip and node. The Kotlin compiler is downloaded once into CACHE
# (default ~/.cache/expo-ntfy-compile, shared with js/scripts/compile-service.sh) and checked against
# its SHA-256 sum. The platform is ANDROID_JAR, or platforms/<ANDROID_PLATFORM, default android-36>/
# android.jar under ANDROID_HOME, ANDROID_SDK_ROOT or ~/Library/Android/sdk.
#
# Prints "compiled: <classes>" and exits 0, or prints the compiler's errors and exits 1.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
CACHE=${CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/expo-ntfy-compile}
PLATFORM=${ANDROID_PLATFORM:-android-36}
KOTLIN=2.1.20
ZIP=$CACHE/kotlin-compiler-$KOTLIN.zip
SUM=a118197b0de55ffab2bc8d5cd03a5e39033cfb53383d6931bc761dec0784891a
if ! { [ -f "$ZIP" ] && echo "$SUM  $ZIP" | shasum -a 256 -c - >/dev/null 2>&1; }; then
  mkdir -p "$CACHE"
  curl -sfL -o "$ZIP.part" "https://github.com/JetBrains/kotlin/releases/download/v$KOTLIN/kotlin-compiler-$KOTLIN.zip"
  echo "$SUM  $ZIP.part" | shasum -a 256 -c - >/dev/null 2>&1 || { echo "checksum mismatch" >&2; exit 1; }
  mv "$ZIP.part" "$ZIP"
fi
KOTLINC=$CACHE/kotlinc-$KOTLIN/kotlinc/bin/kotlinc
[ -x "$KOTLINC" ] || unzip -q -o "$ZIP" -d "$CACHE/kotlinc-$KOTLIN"
if [ -z "${ANDROID_JAR:-}" ]; then
  for sdk in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" "$HOME/Library/Android/sdk"; do
    if [ -n "$sdk" ] && [ -f "$sdk/platforms/$PLATFORM/android.jar" ]; then
      ANDROID_JAR=$sdk/platforms/$PLATFORM/android.jar; break
    fi
  done
fi
[ -f "${ANDROID_JAR:-}" ] || { echo "no android.jar: set ANDROID_JAR, or install platforms;$PLATFORM" >&2; exit 1; }
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/src"
if [ $# -gt 0 ]; then
  cp "$1" "$W/src/AdbKeeper.kt"
else
  (cd "$HERE" && node -e '
    const p = require("./app.plugin.js");
    process.stdout.write(p.renderKotlin(p.resolveOptions({ android: { package: "org.example.app" } }, {})));') \
    > "$W/src/AdbKeeper.kt"
fi
PKG=$(sed -n 's/^package \([A-Za-z0-9_.]*\).*/\1/p' "$W/src/AdbKeeper.kt" | head -1)
"$KOTLINC" -nowarn -jvm-target 17 -cp "$ANDROID_JAR" -d "$W/out" "$W/src/AdbKeeper.kt" 2>&1 | grep -v '^warning:' || true
OUT="$W/out/$(echo "$PKG" | tr . /)"
[ -f "$OUT/AdbKeeperJob.class" ] || { echo "not compiled"; exit 1; }
echo "compiled: $(ls "$OUT" | tr '\n' ' ')"
