#!/usr/bin/env bash
# Checks that every 64-bit native library in an APK or an Android App Bundle
# (.aab) is ready for 16 KB memory pages, which Google Play requires for apps
# targeting Android 15 and later:
#   1. each ELF LOAD segment of the arm64-v8a and x86_64 libraries is aligned
#      to 2**14 (16 KB) or more (llvm-readelf from the NDK), and
#   2. for an APK, each uncompressed .so starts on a 16 KB boundary in the zip
#      (zipalign -c -P 16 from build-tools 35 or newer). A bundle is not
#      zip-aligned the way an APK is, so this step is skipped for .aab files.
# 32-bit libraries are listed for information only; the rule does not apply
# to them. The check fails when the file holds no native libraries at all.
#
# Usage: mobile/scripts/check-16kb-alignment.sh [path/to/app.apk|app.aab]
# Default: the newest of the release APKs (mobile/app/build/outputs/apk/release/*.apk)
# and release bundles (mobile/app/build/outputs/bundle/release/*.aab).
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
artifact="${1:-}"
if [[ -z "$artifact" ]]; then
  # ls -t lists the newest file first.
  artifact="$(ls -t "$here"/app/build/outputs/apk/release/*.apk "$here"/app/build/outputs/bundle/release/*.aab 2>/dev/null | head -1 || true)"
fi
if [[ -z "$artifact" || ! -f "$artifact" ]]; then
  echo "No APK or AAB found. Run ./gradlew assembleRelease bundleRelease first, or pass the path." >&2
  exit 2
fi
case "$artifact" in
  *.aab) kind="aab"; libroot="base/lib" ;;
  *.apk) kind="apk"; libroot="lib" ;;
  *) echo "Expected an .apk or .aab file: $artifact" >&2; exit 2 ;;
esac

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
unzip -q -o "$artifact" "$libroot/*" -d "$work" 2>/dev/null || true

status=0
found=0
echo "$(echo "$kind" | tr a-z A-Z): $artifact"
for so in "$work/$libroot"/*/*.so; do
  [[ -f "$so" ]] || continue
  found=$((found + 1))
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

if (( found == 0 )); then
  echo "FAILED: no native libraries found under $libroot/ in $artifact" >&2
  status=1
fi

if [[ "$kind" == "aab" ]]; then
  echo "zip alignment: not checked for a bundle"
elif [[ -n "$zipalign" ]]; then
  if "$zipalign" -c -P 16 4 "$artifact" >/dev/null; then
    echo "zipalign -c -P 16: ok"
  else
    echo "zipalign -c -P 16: FAILED (uncompressed libraries are not on 16 KB boundaries)"
    status=1
  fi
else
  echo "zipalign not found; zip alignment not checked." >&2
fi

exit "$status"
