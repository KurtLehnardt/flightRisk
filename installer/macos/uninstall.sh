#!/usr/bin/env bash
# Uninstall FlightRisk from this Mac (an install made by installer/macos/install.sh).
#
#   uninstall.sh                 ask, then uninstall
#   uninstall.sh --yes           uninstall without asking
#   uninstall.sh --yes --force   ...also when the folder holds changes that are not on GitHub
#   uninstall.sh --check         print one JSON line saying what it would do; change nothing
#   uninstall.sh --no-keep-data  do not keep a copy of logs, captures and models (see below)
#
# What it removes: the install folder (~/Library/Application Support/FlightRisk/app,
# with its virtual environment), the rest of that FlightRisk folder that the
# installer and the app wrote (status, run files), ~/Applications/FlightRisk.app
# when it is FlightRisk's (same bundle id), and ~/Library/Logs/FlightRisk.
#
# What it keeps: ~/.flightrisk (launcher presets, the saved ceiling),
# ~/.flightrisk-fleet.env (the edge token), Homebrew, git and Python. Before the
# folder is deleted, what FlightRisk wrote into it (logs/, captures/,
# recordings/, models/, flightrisk_sessions.db, target_reference.*) is copied to
# ~/Documents/FlightRisk data <date>/, unless --no-keep-data is given. Captures
# can show people, so delete that copy when you no longer need it.
#
# What it refuses (and then deletes nothing):
#   exit 2  the folder is a link, is not a FlightRisk checkout, or has no
#           .git/flightrisk-installer marker (someone's own checkout is never deleted,
#           not even with --force)
#   exit 3  FlightRisk is running from the folder (it never stops FlightRisk,
#           because an edge may be flying a drone; quit it from the launcher window)
#   exit 4  the folder holds changes that are not on GitHub, and --force was not given
#   exit 1  cancelled at the question
# An install folder that was already deleted by hand is not a refusal: the rest
# is still removed.
set -euo pipefail

FLIGHTRISK_HOME="${FLIGHTRISK_HOME:-$HOME/Library/Application Support/FlightRisk}"
APP_DIR="$FLIGHTRISK_HOME/app"
VENV_DIR="$APP_DIR/venv"
MARKER="$APP_DIR/.git/flightrisk-installer"
APPLICATIONS_DIR="${FLIGHTRISK_APPLICATIONS_DIR:-$HOME/Applications}"
APP_BUNDLE="$APPLICATIONS_DIR/FlightRisk.app"
LOG_DIR="${FLIGHTRISK_LOG_DIR:-$HOME/Library/Logs/FlightRisk}"
BUNDLE_ID="io.github.kurtlehnardt.flightrisk.launcher"
KEEP_ITEMS=(logs captures recordings models flightrisk_sessions.db flightrisk_sessions.db-wal flightrisk_sessions.db-shm)

YES=0
FORCE=0
CHECK=0
KEEP_DATA=1
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1; shift ;;
    --force) FORCE=1; shift ;;
    --check) CHECK=1; shift ;;
    --keep-data) KEEP_DATA=1; shift ;;
    --no-keep-data) KEEP_DATA=0; shift ;;
    *) echo "uninstall.sh: unknown option $1" >&2; exit 64 ;;
  esac
done

json_string() {
  printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"
}

json_list() {
  local first=1 item
  printf '['
  for item in "$@"; do
    [ "$first" = "1" ] || printf ','
    json_string "$item"
    first=0
  done
  printf ']'
}

# A JSON list of the numbers given as arguments.
json_numbers() {
  local IFS=,
  printf '[%s]' "$*"
}

bool() { if "$@"; then printf true; else printf false; fi; }

is_checkout() {
  [ -d "$APP_DIR/.git" ] && [ -f "$APP_DIR/flightrisk/__init__.py" ]
}

is_our_bundle() {
  local id
  [ -f "$APP_BUNDLE/Contents/Info.plist" ] || return 1
  id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP_BUNDLE/Contents/Info.plist" 2>/dev/null || true)"
  [ "$id" = "$BUNDLE_ID" ]
}

running_pids() {
  ps -axww -o pid=,args= 2>/dev/null | awk -v venv="$VENV_DIR/" -v me="$$" '
    { pid = $1; line = $0; sub(/^[ \t]*[0-9]+[ \t]+/, "", line)
      if (pid != me && index(line, venv) == 1) print pid }'
}

# Work in the folder that is not on GitHub: changed tracked files, commits on
# no remote branch or tag, and stashes.
unsaved_work() {
  local changes commits stashes
  changes="$(git -C "$APP_DIR" status --porcelain --untracked-files=no 2>/dev/null || true)"
  [ -n "$changes" ] && printf 'changed files: %s\n' "$(printf '%s\n' "$changes" | wc -l | tr -d ' ')"
  commits="$(git -C "$APP_DIR" log HEAD --branches --not --remotes --tags --oneline 2>/dev/null || true)"
  [ -n "$commits" ] && printf 'commits that are not pushed: %s\n' "$(printf '%s\n' "$commits" | wc -l | tr -d ' ')"
  stashes="$(git -C "$APP_DIR" stash list 2>/dev/null || true)"
  [ -n "$stashes" ] && printf 'stashes: %s\n' "$(printf '%s\n' "$stashes" | wc -l | tr -d ' ')"
  return 0
}

kept_items() {
  local item
  for item in "${KEEP_ITEMS[@]}"; do
    [ -e "$APP_DIR/$item" ] && printf '%s\n' "$item"
  done
  for item in "$APP_DIR"/target_reference.*; do
    [ -e "$item" ] && printf '%s\n' "$(basename -- "$item")"
  done
  return 0
}

refuse() {
  printf '{"removed":false,"reason":"%s","installDir":%s}\n' "$1" "$(json_string "$APP_DIR")"
  printf 'uninstall.sh: %s\n' "$2" >&2
  exit "$3"
}

UNSAVED=()
KEEP=()
if [ -d "$APP_DIR" ] && [ ! -L "$APP_DIR" ]; then
  while IFS= read -r line; do [ -n "$line" ] && UNSAVED+=("$line"); done < <(unsaved_work)
  while IFS= read -r line; do [ -n "$line" ] && KEEP+=("$line"); done < <(kept_items)
fi
RUNNING="$(running_pids | tr '\n' ' ')"
RUNNING="${RUNNING% }"

if [ "$CHECK" = "1" ]; then
  printf '{"check":true,"installDir":%s,"exists":%s,"isLink":%s,"flightriskCheckout":%s,"installerMade":%s,"running":%s,"unsaved":%s,"keep":%s,"app":%s,"appIsOurs":%s}\n' \
    "$(json_string "$APP_DIR")" "$(bool test -d "$APP_DIR")" "$(bool test -L "$APP_DIR")" "$(bool is_checkout)" \
    "$(bool test -f "$MARKER")" "$(json_numbers ${RUNNING:+$RUNNING})" "$(json_list ${UNSAVED+"${UNSAVED[@]}"})" \
    "$(json_list ${KEEP+"${KEEP[@]}"})" "$(json_string "$APP_BUNDLE")" "$(bool is_our_bundle)"
  exit 0
fi

cd /
ALREADY_GONE=0
if [ -L "$APP_DIR" ]; then
  refuse "is-a-link" "$APP_DIR is a link to another folder; nothing was deleted." 2
elif [ ! -e "$APP_DIR" ]; then
  ALREADY_GONE=1
elif ! is_checkout; then
  refuse "not-a-flightrisk-install" "$APP_DIR does not look like a FlightRisk install; nothing was deleted." 2
elif [ ! -f "$MARKER" ]; then
  refuse "not-made-by-installer" "$APP_DIR was not made by the FlightRisk installer, so it was not deleted. Delete it yourself if you are sure." 2
fi
if [ -n "$RUNNING" ]; then
  refuse "running" "FlightRisk is running from $APP_DIR (process $RUNNING). Land the drones, stop everything in the launcher window and quit it, then run this again." 3
fi
if [ "${#UNSAVED[@]}" -gt 0 ] && [ "$FORCE" != "1" ]; then
  if [ "$YES" = "1" ] || [ ! -t 0 ]; then
    refuse "unsaved-work" "$APP_DIR holds work that is not on GitHub (${UNSAVED[*]}). Run again with --force to delete it anyway." 4
  fi
fi

if [ "$YES" != "1" ]; then
  if [ ! -t 0 ]; then
    echo "uninstall.sh: no terminal to ask in; run with --yes to uninstall without asking." >&2
    exit 1
  fi
  echo "This removes FlightRisk from this Mac:"
  [ "$ALREADY_GONE" = "1" ] || echo "  $APP_DIR (with its Python packages)"
  is_our_bundle && echo "  $APP_BUNDLE"
  echo "  $LOG_DIR"
  if [ "${#UNSAVED[@]}" -gt 0 ]; then echo "  and work that is not on GitHub: ${UNSAVED[*]}"; fi
  if [ "$KEEP_DATA" = "1" ] && [ "${#KEEP[@]}" -gt 0 ]; then
    echo "A copy of ${KEEP[*]} is kept in your Documents folder."
  fi
  echo "Kept: ~/.flightrisk, ~/.flightrisk-fleet.env, Homebrew, git and Python."
  read -r -p "Type yes to uninstall: " answer
  [ "$answer" = "yes" ] || { echo "Cancelled; nothing was changed."; exit 1; }
fi

KEPT_DIR=""
if [ "$ALREADY_GONE" != "1" ] && [ "$KEEP_DATA" = "1" ] && [ "${#KEEP[@]}" -gt 0 ]; then
  KEPT_DIR="$HOME/Documents/FlightRisk data $(date +%Y-%m-%d-%H%M%S)"
  mkdir -p -- "$KEPT_DIR"
  for item in "${KEEP[@]}"; do
    cp -R -p -- "$APP_DIR/$item" "$KEPT_DIR/" \
      || { echo "uninstall.sh: could not copy $item to $KEPT_DIR; nothing was deleted." >&2; exit 1; }
  done
fi

if [ "$ALREADY_GONE" != "1" ]; then
  # Moved aside first, so a delete that stops half-way never leaves a broken
  # install in the place the app looks for one.
  trash="$FLIGHTRISK_HOME/.removing-$$"
  mv -- "$APP_DIR" "$trash"
  rm -rf -- "$trash"
fi
REMOVED_APP=false
if is_our_bundle; then
  rm -rf -- "$APP_BUNDLE"
  REMOVED_APP=true
fi
rm -rf -- "$FLIGHTRISK_HOME/run" "$FLIGHTRISK_HOME/install.lock.d"
rm -f -- "$FLIGHTRISK_HOME/install-status.json"
rmdir -- "$FLIGHTRISK_HOME" 2>/dev/null || true
rm -rf -- "$LOG_DIR"

printf '{"removed":true,"alreadyGone":%s,"installDir":%s,"appRemoved":%s,"keptData":%s}\n' \
  "$([ "$ALREADY_GONE" = "1" ] && printf true || printf false)" "$(json_string "$APP_DIR")" "$REMOVED_APP" \
  "$(if [ -n "$KEPT_DIR" ]; then json_string "$KEPT_DIR"; else printf null; fi)"
echo "FlightRisk was uninstalled. ~/.flightrisk and ~/.flightrisk-fleet.env were kept." >&2
