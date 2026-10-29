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
# with its virtual environment); the files the installer and the app wrote next
# to it (status, lock, run files, an unfinished first download), each by name;
# FlightRisk.app in ~/Applications and in /Applications when it is FlightRisk's
# (same bundle id; one in /Applications that you cannot delete is named, so you
# can drag it to the Trash); and the app's log files in ~/Library/Logs/FlightRisk.
# Folders are removed only when they are then empty.
#
# What it keeps: ~/.flightrisk (launcher presets, the saved ceiling),
# ~/.flightrisk-fleet.env (the edge token), Homebrew, git and Python. Before the
# folder is deleted, what FlightRisk wrote into it (logs/, captures/,
# recordings/, models/, flightrisk_sessions.db, target_reference.*) is copied to
# ~/Library/Application Support/FlightRisk data/<date>/, unless --no-keep-data is
# given. That folder stays on this Mac (it is not in Documents, which iCloud may
# sync); captures can show people, so delete the copy when you no longer need it.
#
# What it refuses (and then deletes nothing):
#   exit 2  the folder is a link, is not a FlightRisk checkout, or has no
#           .git/flightrisk-installer marker (someone's own checkout is never deleted,
#           not even with --force); or FLIGHTRISK_HOME or FLIGHTRISK_LOG_DIR is a
#           shared folder such as your home folder
#   exit 3  FlightRisk is running from the folder, or an install or update is
#           running (it never stops FlightRisk, because an edge may be flying a
#           drone; quit it from the launcher window)
#   exit 4  the folder holds changes that are not on GitHub, and --force was not given
#   exit 1  cancelled at the question
# An install folder that was already deleted by hand is not a refusal: the rest
# is still removed.
set -euo pipefail

FLIGHTRISK_HOME="${FLIGHTRISK_HOME:-$HOME/Library/Application Support/FlightRisk}"
FLIGHTRISK_HOME="${FLIGHTRISK_HOME%/}"
APP_DIR="$FLIGHTRISK_HOME/app"
MARKER="$APP_DIR/.git/flightrisk-installer"
LOCK_LINK="$FLIGHTRISK_HOME/install.lock"
APPLICATIONS_DIR="${FLIGHTRISK_APPLICATIONS_DIR:-$HOME/Applications}"
SYSTEM_APPLICATIONS_DIR="${FLIGHTRISK_SYSTEM_APPLICATIONS_DIR:-/Applications}"
LOG_DIR="${FLIGHTRISK_LOG_DIR:-$HOME/Library/Logs/FlightRisk}"
LOG_DIR="${LOG_DIR%/}"
KEEP_ROOT="$HOME/Library/Application Support/FlightRisk data"
BUNDLE_ID="io.github.kurtlehnardt.flightrisk.launcher"
KEEP_ITEMS=(logs captures recordings models flightrisk_sessions.db flightrisk_sessions.db-wal flightrisk_sessions.db-shm)
RUN_FILES=(launcher.pid launcher.url open-launcher.html install.sh install-flightrisk.command)
LOG_FILES=(app.log launcher.log launcher.previous.log)

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

# --- shared with uninstall.sh: begin (a test checks that both copies match) ---

# The start time of process $1 as ps prints it, or nothing when it does not run.
process_start() {
  ps -o lstart= -p "$1" 2>/dev/null | sed 's/^ *//; s/ *$//' || true
}

# Whether the install lock $1 is held by a running install. The lock is a
# symbolic link whose target text is "pid|start time": the pid must run and
# have started at that time (so a reused pid does not count).
lock_owner_alive() {
  local owner pid start
  owner="$(readlink "$1" 2>/dev/null)" || return 1
  pid="${owner%%|*}"
  start="${owner#*|}"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [ "$(process_start "$pid")" = "$start" ]
}

# Process ids of programs that run FlightRisk from this install, one per line:
# a working directory or a loaded file inside the install folder, the install
# folder on the command line, or the launcher that FlightRisk.app recorded.
# The working directory is what catches a Homebrew or python.org Python, whose
# command line names Python.app rather than the virtual environment: the app,
# scripts/launcher.command and the launcher's own children all start in the
# install folder. A shell that merely sits in the folder does not count, and
# neither does this script, its parents or its children.
running_pids() {
  local app real
  app="${APP_DIR%/}"
  real="$(cd -- "$app" 2>/dev/null && pwd -P || true)"
  {
    printf 'S %s\n' "$$"
    ps -axo pid=,ppid=,ucomm= 2>/dev/null | sed 's/^/T /' || true
    ps -axww -o pid=,args= 2>/dev/null | sed 's/^/A /' || true
    if [ -d "$app" ]; then
      lsof -nP -w -a -u "$(id -u)" -d cwd,txt -Fpfn 2>/dev/null | sed 's/^/L /' || true
    fi
    printf 'R %s\n' "$(head -n 1 "$FLIGHTRISK_HOME/run/launcher.pid" 2>/dev/null || true)"
  } | awk -v app="$app" -v real="$real" '
    function inside(p) {
      return p == app || index(p, app "/") == 1 || (real != "" && (p == real || index(p, real "/") == 1))
    }
    function mentions(s) {
      return index(s, app "/") > 0 || (real != "" && index(s, real "/") > 0)
    }
    $1 == "S" { self = $2; next }
    $1 == "T" { parent[$2] = $3; name[$2] = $4; next }
    $1 == "A" { line = $0; sub(/^A[ \t]+[0-9]+[ \t]*/, "", line); args[$2] = line; next }
    $1 == "L" {
      kind = substr($0, 3, 1); value = substr($0, 4)
      if (kind == "p") pid = value
      else if (kind == "f") fd = value
      else if (kind == "n" && inside(value)) { if (fd == "cwd") cwd_in[pid] = 1; else file_in[pid] = 1 }
      next
    }
    $1 == "R" { recorded = $2; next }
    END {
      p = self
      while (p != "" && p != 0 && !(p in mine)) { mine[p] = 1; p = parent[p] }
      for (q in parent) {
        p = q; n = 0
        while (p != "" && p != 0 && n < 100) { if (p == self) { mine[q] = 1; break }; p = parent[p]; n++ }
      }
      for (q in parent) {
        if (q in mine) continue
        shell = name[q] ~ /^-?(sh|bash|zsh|dash|ksh|fish|tcsh|csh|login)$/
        if (file_in[q] || mentions(args[q]) || (cwd_in[q] && !shell) ||
            (q == recorded && index(args[q], "flightrisk.launcher") > 0)) print q
      }
    }' | sort -n
}

# "pid (name), pid (name)" for the process ids on standard input.
describe_pids() {
  local pid list=""
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    list="${list:+$list, }$pid ($(ps -o ucomm= -p "$pid" 2>/dev/null | sed 's/ *$//' || true))"
  done
  printf '%s' "$list"
}

# Refuse a FLIGHTRISK_HOME that would put the install in a shared place.
unsafe_home() {
  local dir="${1%/}"
  [ -z "$dir" ] || [ "$dir" = "/" ] || [ "$dir" = "${HOME%/}" ] || [ "$dir" = "/Applications" ] ||
    [ "$dir" = "${HOME%/}/Library" ] || [ "$dir" = "${HOME%/}/Library/Application Support" ]
}

# --- shared with uninstall.sh: end ---

is_checkout() {
  [ -d "$APP_DIR/.git" ] && [ -f "$APP_DIR/flightrisk/__init__.py" ]
}

# Whether $1 is FlightRisk's app bundle (by bundle id, not by name).
is_our_bundle() {
  local id
  [ -f "$1/Contents/Info.plist" ] && [ ! -L "$1" ] || return 1
  id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$1/Contents/Info.plist" 2>/dev/null || true)"
  [ "$id" = "$BUNDLE_ID" ]
}

our_bundles() {
  local dir
  for dir in "$APPLICATIONS_DIR" "$SYSTEM_APPLICATIONS_DIR"; do
    if is_our_bundle "$dir/FlightRisk.app"; then printf '%s\n' "$dir/FlightRisk.app"; fi
  done
  return 0
}

# Work in the folder that is not on GitHub: changed tracked files, commits on
# no remote branch or tag, and stashes. Files that an interrupted update of the
# installer left changed (the marker says "pending") are the installer's, not yours.
unsaved_work() {
  local changes commits stashes
  if ! grep -q '^pending=' "$MARKER" 2>/dev/null; then
    changes="$(git -C "$APP_DIR" status --porcelain --untracked-files=no 2>/dev/null || true)"
    [ -n "$changes" ] && printf 'changed files: %s\n' "$(printf '%s\n' "$changes" | wc -l | tr -d ' ')"
  fi
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

unsafe_log_dir() {
  local dir="${1%/}"
  [ -z "$dir" ] || [ "$dir" = "/" ] || [ "$dir" = "${HOME%/}" ] || [ "$dir" = "${HOME%/}/Library" ] ||
    [ "$dir" = "${HOME%/}/Library/Logs" ]
}

refuse() {
  printf '{"removed":false,"reason":"%s","installDir":%s}\n' "$1" "$(json_string "$APP_DIR")"
  printf 'uninstall.sh: %s\n' "$2" >&2
  exit "$3"
}

if unsafe_home "$FLIGHTRISK_HOME" || unsafe_log_dir "$LOG_DIR"; then
  refuse "unsafe-folder" "FLIGHTRISK_HOME ($FLIGHTRISK_HOME) or FLIGHTRISK_LOG_DIR ($LOG_DIR) is a shared folder; nothing was deleted." 2
fi

UNSAVED=()
KEEP=()
if [ -d "$APP_DIR" ] && [ ! -L "$APP_DIR" ]; then
  while IFS= read -r line; do [ -n "$line" ] && UNSAVED+=("$line"); done < <(unsaved_work)
  while IFS= read -r line; do [ -n "$line" ] && KEEP+=("$line"); done < <(kept_items)
fi
RUNNING=()
while IFS= read -r line; do [ -n "$line" ] && RUNNING+=("$line"); done < <(running_pids)
BUNDLES=()
while IFS= read -r line; do [ -n "$line" ] && BUNDLES+=("$line"); done < <(our_bundles)
INSTALLING=false
if lock_owner_alive "$LOCK_LINK"; then INSTALLING=true; fi

if [ "$CHECK" = "1" ]; then
  printf '{"check":true,"installDir":%s,"exists":%s,"isLink":%s,"flightriskCheckout":%s,"installerMade":%s,"running":%s,"installing":%s,"unsaved":%s,"keep":%s,"apps":%s}\n' \
    "$(json_string "$APP_DIR")" "$(bool test -d "$APP_DIR")" "$(bool test -L "$APP_DIR")" "$(bool is_checkout)" \
    "$(bool test -f "$MARKER")" "$(json_numbers ${RUNNING+"${RUNNING[@]}"})" "$INSTALLING" \
    "$(json_list ${UNSAVED+"${UNSAVED[@]}"})" "$(json_list ${KEEP+"${KEEP[@]}"})" \
    "$(json_list ${BUNDLES+"${BUNDLES[@]}"})"
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
if [ "$INSTALLING" = "true" ]; then
  refuse "install-running" "A FlightRisk install or update is running (pid $(readlink "$LOCK_LINK" 2>/dev/null | cut -d'|' -f1)). Let it finish, then run this again." 3
fi
if [ "${#RUNNING[@]}" -gt 0 ]; then
  refuse "running" "FlightRisk is running from $APP_DIR (process $(printf '%s\n' "${RUNNING[@]}" | describe_pids)). Land the drones, stop everything in the launcher window and quit it, then run this again." 3
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
  for bundle in ${BUNDLES+"${BUNDLES[@]}"}; do echo "  $bundle"; done
  echo "  the app's logs in $LOG_DIR"
  if [ "${#UNSAVED[@]}" -gt 0 ]; then echo "  and work that is not on GitHub: ${UNSAVED[*]}"; fi
  if [ "$KEEP_DATA" = "1" ] && [ "${#KEEP[@]}" -gt 0 ]; then
    echo "A copy of ${KEEP[*]} is kept in $KEEP_ROOT (captures can show people; delete it when done)."
  fi
  echo "Kept: ~/.flightrisk, ~/.flightrisk-fleet.env, Homebrew, git and Python."
  read -r -p "Type yes to uninstall: " answer
  [ "$answer" = "yes" ] || { echo "Cancelled; nothing was changed."; exit 1; }
fi

KEPT_DIR=""
if [ "$ALREADY_GONE" != "1" ] && [ "$KEEP_DATA" = "1" ] && [ "${#KEEP[@]}" -gt 0 ]; then
  KEPT_DIR="$KEEP_ROOT/$(date +%Y-%m-%d-%H%M%S)"
  mkdir -p -- "$KEEP_ROOT"
  chmod 700 "$KEEP_ROOT"
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

REMOVED_APPS=()
LEFT_APPS=()
for bundle in ${BUNDLES+"${BUNDLES[@]}"}; do
  if rm -rf -- "$bundle" 2>/dev/null && [ ! -e "$bundle" ]; then
    REMOVED_APPS+=("$bundle")
  else
    LEFT_APPS+=("$bundle")
    echo "uninstall.sh: $bundle could not be deleted; drag it to the Trash yourself." >&2
  fi
done

# Only the files the installer and the app write, by name; then the folders, if empty.
for name in "${RUN_FILES[@]}"; do rm -f -- "$FLIGHTRISK_HOME/run/$name"; done
rmdir -- "$FLIGHTRISK_HOME/run" 2>/dev/null || true
rm -f -- "$FLIGHTRISK_HOME/install-status.json"
if [ -L "$LOCK_LINK" ]; then rm -f -- "$LOCK_LINK"; fi
for incoming in "$FLIGHTRISK_HOME"/.app-incoming.*; do
  if [ -d "$incoming/.git" ]; then rm -rf -- "$incoming"; fi
done
rmdir -- "$FLIGHTRISK_HOME" 2>/dev/null || true
for name in "${LOG_FILES[@]}"; do rm -f -- "$LOG_DIR/$name"; done
rmdir -- "$LOG_DIR" 2>/dev/null || true

printf '{"removed":true,"alreadyGone":%s,"installDir":%s,"appsRemoved":%s,"appsLeft":%s,"keptData":%s}\n' \
  "$([ "$ALREADY_GONE" = "1" ] && printf true || printf false)" "$(json_string "$APP_DIR")" \
  "$(json_list ${REMOVED_APPS+"${REMOVED_APPS[@]}"})" "$(json_list ${LEFT_APPS+"${LEFT_APPS[@]}"})" \
  "$(if [ -n "$KEPT_DIR" ]; then json_string "$KEPT_DIR"; else printf null; fi)"
echo "FlightRisk was uninstalled. ~/.flightrisk and ~/.flightrisk-fleet.env were kept." >&2
