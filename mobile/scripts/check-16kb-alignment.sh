#!/usr/bin/env bash
# Checks that every 64-bit native library in an APK is ready for 16 KB memory
# pages, which Google Play requires for apps targeting Android 15 and later:
#   1. each ELF LOAD segment of lib/arm64-v8a/*.so and lib/x86_64/*.so is
#      aligned to 2**14 (16 KB) or more (llvm-readelf from the NDK), and
#   2. each uncompressed .so starts on a 16 KB boundary in the zip
#      (zipalign -c -P 16 from build-tools 35 or newer).
# 32-bit libraries are listed for information only; the rule does not apply
# to them.
#
# Usage: mobile/scripts/check-16kb-alignment.sh [path/to/app.apk]
# Default APK: mobile/app/build/outputs/apk/release/app-release-unsigned.apk,
# or app-release.apk when the build was signed.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
apk="${1:-}"
if [[ -z "$apk" ]]; then
  for candidate in "$here/app/build/outputs/apk/release/app-release.apk" \
                   "$here/app/build/outputs/apk/release/app-release-unsigned.apk"; do
    if [[ -f "$candidate" ]]; then apk="$candidate"; break; fi
  done
fi
if [[ -z "$apk" || ! -f "$apk" ]]; then
  echo "No APK found. Run ./gradlew assembleRelease first, or pass the APK path." >&2
  exit 2
fi

sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
if [[ -z "$sdk" && -f "$here/local.properties" ]]; then
  sdk="$(sed -n 's/^sdk\.dir=//p' "$here/local.properties")"
fi
readelf="$(ls -d "$sdk"/ndk/*/toolchains/llvm/prebuilt/*/bin/llvm-readelf 2>/dev/null | sort -V | tail -1 || true)"
zipalign="$(ls -d "$sdk"/build-tools/*/zipalign 2>/dev/null | sort -V | tail -1 || true)"
if [[ -z "$readelf" ]]; then
  echo "llvm-readelf not found; install an NDK with the SDK manager." >&2
  exit 2
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
unzip -q -o "$apk" 'lib/*' -d "$work" 2>/dev/null || true

status=0
echo "APK: $apk"
for so in "$work"/lib/*/*.so; do
  [[ -f "$so" ]] || continue
  abi="$(basename "$(dirname "$so")")"
  # The smallest alignment of all LOAD segments, compared as numbers (the values are hex).
  smallest=""
  for align in $("$readelf" -lW "$so" | awk '$1 == "LOAD" { print $NF }'); do
    if [[ -z "$smallest" ]] || (( align < smallest )); then smallest="$align"; fi
  done
  if [[ -n "$smallest" ]] && (( smallest >= 0x4000 )); then result="ok"; else result="NOT 16 KB aligned"; fi
  case "$abi" in
    arm64-v8a|x86_64) [[ "$result" == "ok" ]] || status=1 ;;
    *) result="$result (32-bit, not required)" ;;
  esac
  printf '%-12s %-36s LOAD align %-7s %s\n' "$abi" "$(basename "$so")" "$smallest" "$result"
done

if [[ -n "$zipalign" ]]; then
  if "$zipalign" -c -P 16 4 "$apk" >/dev/null; then
    echo "zipalign -c -P 16: ok"
  else
    echo "zipalign -c -P 16: FAILED (uncompressed libraries are not on 16 KB boundaries)"
    status=1
  fi
else
  echo "zipalign not found; zip alignment not checked." >&2
fi

exit "$status"
