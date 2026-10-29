#!/usr/bin/env bash
# Build the macOS disk image: FlightRisk.app, a shortcut to /Applications and a
# short "Read me first" note, in a compressed, read-only .dmg.
#
#   bash installer/macos/build-dmg.sh [--out DIR] [--ref vX.Y.Z] [--no-sign]
#
# --out    folder for the image (default: dist/ in the repository)
# --ref    the release the app's bundled installer installs; default: the
#          release tag at HEAD if there is one, else none (the newest release,
#          or main while none is published)
# --no-sign  skip the ad-hoc signature
#
# The app is signed ad hoc ("codesign --sign -"), which seals the bundle so a
# changed file is detected, but it is not signed with an Apple Developer ID and
# not notarized: Gatekeeper blocks its first launch after a download until the
# user allows it under System Settings, Privacy & Security (see docs/installer.md).
#
# Prints the path of the image it made.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
OUT="$ROOT/dist"
REF=""
SIGN=1

while [ $# -gt 0 ]; do
  case "$1" in
    --out) [ $# -ge 2 ] || { echo "build-dmg.sh: --out needs a folder" >&2; exit 64; }; OUT="$2"; shift 2 ;;
    --ref) [ $# -ge 2 ] || { echo "build-dmg.sh: --ref needs a tag" >&2; exit 64; }; REF="$2"; shift 2 ;;
    --no-sign) SIGN=0; shift ;;
    *) echo "build-dmg.sh: unknown option $1" >&2; exit 64 ;;
  esac
done

[ "$(uname -s)" = "Darwin" ] || { echo "build-dmg.sh: hdiutil exists on macOS only" >&2; exit 1; }
if [ -z "$REF" ]; then
  REF="$(git -C "$ROOT" describe --tags --exact-match --match 'v[0-9]*.[0-9]*.[0-9]*' HEAD 2>/dev/null || true)"
fi
if [ -n "$REF" ] && [[ ! "$REF" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "build-dmg.sh: --ref must be a release tag like v1.2.3 (got '$REF')" >&2
  exit 64
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/flightrisk-dmg.XXXXXX")"
trap 'rm -rf -- "$work"' EXIT
stage="$work/FlightRisk"
mkdir -p -- "$stage"

bash "$SCRIPT_DIR/make-app.sh" build --out "$stage" --ref "$REF" >/dev/null
if [ "$SIGN" = "1" ] && command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - --timestamp=none "$stage/FlightRisk.app"
  codesign --verify --strict "$stage/FlightRisk.app"
fi
ln -s /Applications "$stage/Applications"
cat > "$stage/Read me first.txt" <<'README'
FlightRisk for macOS (Apple Silicon, macOS 14 or newer)

1. Drag FlightRisk to the Applications folder.
2. Open FlightRisk from Applications. The first time, macOS blocks it because it
   is not signed by an Apple Developer account:
   - Open System Settings, Privacy & Security, and scroll down to Security.
   - It names FlightRisk as blocked. Click "Open Anyway" and confirm with Touch ID
     or your password. From then on FlightRisk opens normally.
3. FlightRisk asks to install itself. The install runs in a Terminal window and
   downloads FlightRisk and its Python packages (several GB) into
   ~/Library/Application Support/FlightRisk. When it is done, FlightRisk opens the
   launcher window.

The edge token is never asked for. Put it in ~/.flightrisk-fleet.env as a line
"export FLIGHTRISK_EDGE_TOKEN=..." on each computer that runs a ground station or
edges, as described in docs/fleet-setup.md.

Uninstall: bash ~/Library/Application\ Support/FlightRisk/app/installer/macos/uninstall.sh
README

name="FlightRisk${REF:+-${REF#v}}-arm64.dmg"
mkdir -p -- "$OUT"
hdiutil create -quiet -volname "FlightRisk" -srcfolder "$stage" -fs HFS+ -format UDZO -ov "$OUT/$name"
printf '%s\n' "$OUT/$name"
