#!/usr/bin/env bash
# Update a FlightRisk install made by installer/macos/install.sh.
#
#   update.sh            move the install to the newest release and install its packages
#   update.sh --ref vX.Y.Z   move it to that release (or main)
#   update.sh --check    print one JSON line: the installed release, the newest
#                        release, and whether an update is available; change nothing
#
# The update is the installer run again with FLIGHTRISK_REF set, so it follows
# the installer's rules: it refuses a folder the installer did not make, local
# changes, and a running FlightRisk (it never stops FlightRisk itself, because
# an edge may be flying a drone), and it never moves an install backwards. If
# installing the packages of the new release fails, the folder is put back on
# the release it had.
#
# The installer is read into memory before it runs (bash -c), so the git
# checkout that replaces it on disk cannot change it half-way through.
set -euo pipefail

FLIGHTRISK_HOME="${FLIGHTRISK_HOME:-$HOME/Library/Application Support/FlightRisk}"
APP_DIR="$FLIGHTRISK_HOME/app"
MARKER="$APP_DIR/.git/flightrisk-installer"
CHECK=0
REF_ARG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    --ref) [ $# -ge 2 ] || { echo "update.sh: --ref needs a release tag" >&2; exit 64; }; REF_ARG="$2"; shift 2 ;;
    *) echo "update.sh: unknown option $1" >&2; exit 64 ;;
  esac
done

if [ ! -d "$APP_DIR/.git" ] || [ ! -f "$MARKER" ]; then
  echo "update.sh: $APP_DIR is not a FlightRisk install made by the installer; nothing was changed." >&2
  exit 2
fi

# The installer's own functions (pick_latest_tag, json_string, valid_ref).
# shellcheck disable=SC2034  # read by install.sh
FLIGHTRISK_INSTALL_LIB_ONLY=1
# shellcheck disable=SC1091
. "$APP_DIR/installer/macos/install.sh"
unset FLIGHTRISK_INSTALL_LIB_ONLY
# The repository this install came from, unless FLIGHTRISK_REPO_URL names another.
UPDATE_REPO_URL="${FLIGHTRISK_REPO_URL:-$(git -C "$APP_DIR" remote get-url origin)}"

installed_ref="$(sed -n 's/^ref=//p' "$MARKER" | head -n 1)"
latest="$(git ls-remote --tags --refs "$UPDATE_REPO_URL" | pick_latest_tag)"

if [ "$CHECK" = "1" ]; then
  available=false
  if [ -n "$latest" ] && [ "$latest" != "$installed_ref" ]; then
    if [ "$installed_ref" = "main" ]; then
      available=true
    elif ! git -C "$APP_DIR" merge-base --is-ancestor "$latest" HEAD 2>/dev/null; then
      available=true
    fi
  fi
  printf '{"installDir":%s,"installed":%s,"latest":%s,"updateAvailable":%s}\n' \
    "$(json_string "$APP_DIR")" "$(json_or_null "$installed_ref")" "$(json_or_null "$latest")" "$available"
  exit 0
fi

ref="${REF_ARG:-${latest:-main}}"
valid_ref "$ref" || { echo "update.sh: --ref must be a release tag like v1.2.3, or main (got '$ref')." >&2; exit 64; }
script="$(cat -- "$APP_DIR/installer/macos/install.sh")"
FLIGHTRISK_REF="$ref" FLIGHTRISK_HOME="$FLIGHTRISK_HOME" FLIGHTRISK_REPO_URL="$UPDATE_REPO_URL" \
  exec /bin/bash -c "$script"
