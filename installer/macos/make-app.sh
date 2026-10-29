#!/usr/bin/env bash
# Build FlightRisk.app, the per-user app that starts the FlightRisk launcher.
#
#   make-app.sh install [--dest DIR] [--ref REF]   create or replace DIR/FlightRisk.app
#                                                  (default DIR: ~/Applications)
#   make-app.sh build --out DIR [--ref REF]        the same into any folder (build-dmg.sh)
#
# The bundle holds:
#   Contents/Info.plist              bundle id io.github.kurtlehnardt.flightrisk.launcher
#   Contents/MacOS/FlightRisk        installer/macos/app-main.sh
#   Contents/Resources/FlightRisk.icns
#   Contents/Resources/install.sh    the installer, used when FlightRisk is not installed yet
#   Contents/Resources/install-ref   the release the bundled installer installs (only with --ref vX.Y.Z)
#
# An existing FlightRisk.app is replaced only when it carries the same bundle id,
# so an unrelated app of the same name is never overwritten. The new bundle is
# assembled next to the old one and moved into place, so a failure half-way
# leaves the old one intact.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
BUNDLE_ID="io.github.kurtlehnardt.flightrisk.launcher"
ICNS="$ROOT/installer/assets/FlightRisk.icns"

usage() {
  printf 'usage: make-app.sh install [--dest DIR] [--ref REF] | build --out DIR [--ref REF]\n' >&2
  exit 64
}

xml_escape() {
  printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g'
}

# CFBundleShortVersionString: 1.2.3 for the tag v1.2.3, else 0.0.0.
bundle_version() {
  if [[ "$1" =~ ^v([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
  else
    printf '0.0.0'
  fi
}

emit_info_plist() {
  local version
  version="$(xml_escape "$(bundle_version "$1")")"
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>FlightRisk</string>
  <key>CFBundleDisplayName</key>
  <string>FlightRisk</string>
  <key>CFBundleIdentifier</key>
  <string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key>
  <string>FlightRisk</string>
  <key>CFBundleIconFile</key>
  <string>FlightRisk</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleShortVersionString</key>
  <string>$version</string>
  <key>CFBundleVersion</key>
  <string>$version</string>
  <key>LSMinimumSystemVersion</key>
  <string>14.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <!-- The program is a shell script that starts the launcher and exits, so the
       app shows no Dock icon and no menu bar of its own. -->
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
PLIST
}

is_our_bundle() {
  local id
  [ -f "$1/Contents/Info.plist" ] || return 1
  id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$1/Contents/Info.plist" 2>/dev/null || true)"
  [ "$id" = "$BUNDLE_ID" ]
}

# Assemble FlightRisk.app in folder $1 for release $2.
make_bundle() {
  local dest="$1" ref="$2" app staging contents
  app="$dest/FlightRisk.app"
  if [ -L "$app" ] || { [ -e "$app" ] && ! is_our_bundle "$app"; }; then
    printf "make-app.sh: %s exists and is not FlightRisk's launcher app, so it was left alone\n" "$app" >&2
    return 1
  fi
  mkdir -p -- "$dest"
  staging="$(mktemp -d "$dest/.flightrisk-app.XXXXXX")"
  contents="$staging/FlightRisk.app/Contents"
  if ! {
    mkdir -p -- "$contents/MacOS" "$contents/Resources" &&
    emit_info_plist "$ref" > "$contents/Info.plist" &&
    printf 'APPL????' > "$contents/PkgInfo" &&
    cp -- "$SCRIPT_DIR/app-main.sh" "$contents/MacOS/FlightRisk" &&
    chmod 755 "$contents/MacOS/FlightRisk" &&
    cp -- "$SCRIPT_DIR/install.sh" "$contents/Resources/install.sh" &&
    cp -- "$ICNS" "$contents/Resources/FlightRisk.icns"
  }; then
    rm -rf -- "$staging"
    printf 'make-app.sh: could not assemble the app bundle\n' >&2
    return 1
  fi
  if [[ "$ref" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf '%s\n' "$ref" > "$contents/Resources/install-ref"
  fi
  if [ -e "$app" ]; then rm -rf -- "$app"; fi
  mv -- "$staging/FlightRisk.app" "$app"
  rm -rf -- "$staging"
  # Finder and Launch Services cache bundles by modification time.
  touch -- "$app"
  printf '%s\n' "$app"
}

COMMAND="${1:-}"
[ $# -gt 0 ] && shift
DEST=""
REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dest|--out) [ $# -ge 2 ] || usage; DEST="$2"; shift 2 ;;
    --ref) [ $# -ge 2 ] || usage; REF="$2"; shift 2 ;;
    *) usage ;;
  esac
done

case "$COMMAND" in
  install) make_bundle "${DEST:-$HOME/Applications}" "$REF" >/dev/null ;;
  build) [ -n "$DEST" ] || usage; make_bundle "$DEST" "$REF" ;;
  *) usage ;;
esac
