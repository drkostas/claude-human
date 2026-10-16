#!/usr/bin/env bash
# Compile the Kotlin service the config plugin writes. The plugin's tests read the generated Kotlin
# as text, which shows what it says but not that it compiles, and a published version can never be
# replaced. CI runs this on every pull request.
#
#   scripts/compile-service.sh            render the service with example options and compile it
#   scripts/compile-service.sh FILE.kt    compile a service that a prebuild wrote
#
# Needs Java 17 or later, curl, unzip and node (with `npm ci` done here, for the plugin's own
# imports). The Kotlin compiler and the libraries the plugin adds (androidx core 1.13.1, okhttp
# 4.12.0) are downloaded once into CACHE, default ~/.cache/expo-ntfy-compile, and checked against
# the SHA-256 sums below. The Android platform is ANDROID_JAR, or platforms/<ANDROID_PLATFORM,
# default android-36>/android.jar under ANDROID_HOME, ANDROID_SDK_ROOT or ~/Library/Android/sdk.
#
# Prints "compiled: <classes>" and exits 0, or prints the compiler's errors and exits 1.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
CACHE=${CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/expo-ntfy-compile}
PLATFORM=${ANDROID_PLATFORM:-android-36}
KOTLIN=2.1.20
GOOGLE=https://dl.google.com/android/maven2
CENTRAL=https://repo1.maven.org/maven2

# fetch URL SHA256: the file's path in the cache, downloaded and checked when it is not there yet
fetch() {
  local url=$1 sum=$2 out="$CACHE/${1##*/}"
  if [ -f "$out" ] && echo "$sum  $out" | shasum -a 256 -c - >/dev/null 2>&1; then
    echo "$out"; return
  fi
  mkdir -p "$CACHE"
  curl -sfL -o "$out.part" "$url" || { echo "could not download $url" >&2; exit 1; }
  echo "$sum  $out.part" | shasum -a 256 -c - >/dev/null 2>&1 \
    || { echo "checksum mismatch for $url" >&2; rm -f "$out.part"; exit 1; }
  mv "$out.part" "$out"
  echo "$out"
}

ZIP=$(fetch "https://github.com/JetBrains/kotlin/releases/download/v$KOTLIN/kotlin-compiler-$KOTLIN.zip" \
  a118197b0de55ffab2bc8d5cd03a5e39033cfb53383d6931bc761dec0784891a)
KOTLINC=$CACHE/kotlinc-$KOTLIN/kotlinc/bin/kotlinc
[ -x "$KOTLINC" ] || unzip -q -o "$ZIP" -d "$CACHE/kotlinc-$KOTLIN"
CORE=$(fetch "$GOOGLE/androidx/core/core/1.13.1/core-1.13.1.aar" \
  2c27de199535675005553066597a4b20fa1eea7c228ab4ef6b32b5fe39ca1f59)
ANNOTATION=$(fetch "$GOOGLE/androidx/annotation/annotation-jvm/1.8.1/annotation-jvm-1.8.1.jar" \
  9aab326d9492800991854360ac248f493ce7f7c3183519309b78ace9e240f6f6)
OKHTTP=$(fetch "$CENTRAL/com/squareup/okhttp3/okhttp/4.12.0/okhttp-4.12.0.jar" \
  b1050081b14bb7a3a7e55a4d3ef01b5dcfabc453b4573a4fc019767191d5f4e0)
OKIO=$(fetch "$CENTRAL/com/squareup/okio/okio-jvm/3.6.0/okio-jvm-3.6.0.jar" \
  67543f0736fc422ae927ed0e504b98bc5e269fda0d3500579337cb713da28412)

if [ -z "${ANDROID_JAR:-}" ]; then
  for sdk in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" "$HOME/Library/Android/sdk"; do
    if [ -n "$sdk" ] && [ -f "$sdk/platforms/$PLATFORM/android.jar" ]; then
      ANDROID_JAR=$sdk/platforms/$PLATFORM/android.jar; break
    fi
  done
fi
[ -f "${ANDROID_JAR:-}" ] || { echo "no android.jar: set ANDROID_JAR, or install platforms;$PLATFORM in the SDK" >&2; exit 1; }

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/src" "$W/core"
if [ $# -gt 0 ]; then
  cp "$1" "$W/src/NtfyService.kt"
else
  (cd "$HERE" && node -e '
    const p = require("./app.plugin.js");
    const o = p.resolveOptions({ name: "Example", scheme: "example", android: { package: "org.example.app" } },
                               { url: "https://ntfy.example.org/alerts", subTextTagPrefix: "area:" });
    process.stdout.write(p.renderKotlin(o));') > "$W/src/NtfyService.kt"
fi
# The service reads its topic from the app's BuildConfig, which only an app build generates.
PKG=$(sed -n 's/^package \([A-Za-z0-9_.]*\).*/\1/p' "$W/src/NtfyService.kt" | head -1)
# Every field the service reads (the topic URL and the access token) gets a stand-in value.
FIELDS=$(grep -o 'BuildConfig\.[A-Z_][A-Z0-9_]*' "$W/src/NtfyService.kt" | cut -d. -f2 | sort -u)
{ printf 'package %s\nobject BuildConfig {\n' "$PKG"
  for f in $FIELDS; do printf '    const val %s = "https://ntfy.example.org/alerts"\n' "$f"; done
  printf '}\n'; } > "$W/src/BuildConfig.kt"
unzip -q -o "$CORE" classes.jar -d "$W/core"
CP="$ANDROID_JAR:$W/core/classes.jar:$ANNOTATION:$OKHTTP:$OKIO"
"$KOTLINC" -nowarn -jvm-target 17 -cp "$CP" -d "$W/out" "$W/src/NtfyService.kt" "$W/src/BuildConfig.kt" 2>&1 \
  | grep -v '^warning:' || true
OUT="$W/out/$(echo "$PKG" | tr . /)"
[ -f "$OUT/NtfyService.class" ] || { echo "not compiled"; exit 1; }
echo "compiled: $(ls "$OUT" | tr '\n' ' ')"
