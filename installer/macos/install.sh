#!/usr/bin/env bash
# FlightRisk -- per-user installer for macOS.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/KurtLehnardt/flightRisk/main/installer/macos/install.sh)"
#
# Run it as `bash -c "$(curl ...)"`, not `curl ... | bash`. With a pipe, bash reads
# the script from standard input a piece at a time, and every command that also
# reads standard input (Homebrew does) can swallow the rest of the script, which
# then ends early without an error. As an argument, the whole script is read
# before it starts.
#
# What it does, each step skipped when it is already done, so it is safe to run
# again (that is also how an install is repaired or updated):
#
#   1. Checks for git and Python 3.13 and installs what is missing with Homebrew
#      (Homebrew itself is installed first if it is missing; that one step asks
#      for an administrator password, everything else runs as you).
#   2. Downloads FlightRisk into ~/Library/Application Support/FlightRisk/app:
#      the newest release (a tag like v1.2.3), or main while no release exists.
#      FLIGHTRISK_REF=v1.2.3 (or main) picks one. An existing install is moved to
#      it, but never backwards, never over local changes, never while FlightRisk
#      is running, and never when the folder was not made by this installer.
#   3. Creates the virtual environment app/venv and installs the pinned packages
#      of installer/locks/macos-arm64.txt (several GB: PyTorch, Ultralytics,
#      OpenCV, MAVSDK and the rest): wheels only, each checked against its
#      SHA-256 in the lock, from PyPI only (pip's own settings and PIP_*
#      variables are ignored).
#   4. Downloads the yolo11n detector weights and checks their SHA-256. The
#      fine-tuned HERIDAL weights (models/heridal_yolo11s.pt) are not published;
#      copy them into app/models yourself if you have them.
#   5. Creates ~/Applications/FlightRisk.app, which starts the launcher window.
#
# An interrupted run (Ctrl+C, a closed window, a kill) can always be repaired by
# running this again: a first download is made in a separate folder and only
# moved into place when it is complete, and an update marks itself as pending in
# the install's marker, so a later run finishes it.
#
# It never asks for, stores or prints the edge token. The launcher reads it from
# FLIGHTRISK_EDGE_TOKEN or ~/.flightrisk-fleet.env, as before. ~/.flightrisk
# (presets and the saved ceiling) is not touched.
#
# Progress goes to a status file, by default
# ~/Library/Application Support/FlightRisk/install-status.json, as one JSON
# object: {"state": "running"|"done"|"error", "step": "...", "message": ...,
# "ref": ..., "pid": N, "at": "..."}. A lock next to it (install.lock) keeps two
# installs from running at once.
#
# Settings (environment variables, all optional):
#   FLIGHTRISK_REF               release tag (v1.2.3) or main; default: the newest release
#   FLIGHTRISK_HOME              install root; default ~/Library/Application Support/FlightRisk
#   FLIGHTRISK_APPLICATIONS_DIR  where FlightRisk.app goes; default ~/Applications
#   FLIGHTRISK_PYTHON            a Python 3.13 to build the virtual environment with
#   FLIGHTRISK_STATUS_FILE       where the status JSON goes
#   FLIGHTRISK_REPO_URL          the repository to install from (tests use a local one)
#   FLIGHTRISK_PIP_INDEX_URL     the package index; default https://pypi.org/simple
#   FLIGHTRISK_ALLOW_DOWNGRADE=1 allow moving an install to an older release
#   FLIGHTRISK_INSTALL_SKIP_DEPS=1  create the virtual environment but install no
#                                packages and download no weights (tests, dry runs)
#   FLIGHTRISK_NO_APP=1          do not create FlightRisk.app
#   FLIGHTRISK_OPEN_WHEN_DONE=1  open FlightRisk.app at the end
set -euo pipefail
set -o errtrace

# A GIT_DIR or GIT_WORK_TREE (or any other GIT_* setting) in the caller's
# environment would point every git command below at another repository.
while IFS= read -r variable; do unset "$variable"; done < <(compgen -e | grep '^GIT_' || true)

FLIGHTRISK_HOME="${FLIGHTRISK_HOME:-$HOME/Library/Application Support/FlightRisk}"
FLIGHTRISK_HOME="${FLIGHTRISK_HOME%/}"
APP_DIR="$FLIGHTRISK_HOME/app"
VENV_DIR="$APP_DIR/venv"
STATUS_PATH="${FLIGHTRISK_STATUS_FILE:-$FLIGHTRISK_HOME/install-status.json}"
LOCK_LINK="$FLIGHTRISK_HOME/install.lock"
LSOF="${FLIGHTRISK_LSOF:-/usr/sbin/lsof}"
INCOMING_PREFIX="$FLIGHTRISK_HOME/.app-incoming."
REPO_URL="${FLIGHTRISK_REPO_URL:-https://github.com/KurtLehnardt/flightRisk.git}"
APPLICATIONS_DIR="${FLIGHTRISK_APPLICATIONS_DIR:-$HOME/Applications}"
PIP_INDEX="${FLIGHTRISK_PIP_INDEX_URL:-https://pypi.org/simple}"
MARKER_NAME="flightrisk-installer"
PYTHON_WANTED="3.13"
LOCK_FILE_MACOS="installer/locks/macos-arm64.txt"
# The Homebrew installer is fetched from a fixed commit and checked against this
# SHA-256 (of the file as served) before it runs, so the bytes executed are known
# in advance. Update both together.
HOMEBREW_INSTALL_COMMIT="8ab1549dfa1189fd4d818a2116592d8f0ee06d8c"
HOMEBREW_INSTALL_SHA256="5f333bbe53bc490e51e7ccb1df8779b3dd6ee73a1a7379efda216edb08ccb148"
# The yolo11n weights are a pickle, so they are checked before anything loads them.
# installer/windows/install.ps1 pins the same file; a test keeps the two in step.
WEIGHTS_URL="https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt"
WEIGHTS_SHA256="0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1"

LOCK_HELD=0
STEP="start"
REF=""
PREV_HEAD=""
PREV_REF=""
MOVED=0
INCOMING_DIR=""
VENV_SNAPSHOT=""
PYTHON_BIN=""
TARGET=""

log()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mok\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

json_or_null() {
  if [ -n "$1" ]; then json_string "$1"; else printf 'null'; fi
}

# Write the status file atomically (a temporary file renamed over it), so a
# reader never sees half a file. A failure to write it never stops the install.
write_status() {
  local state="$1" message="${2:-}" dir tmp
  dir="$(dirname -- "$STATUS_PATH")"
  mkdir -p -- "$dir" 2>/dev/null || return 0
  tmp="$(mktemp "$STATUS_PATH.XXXXXX" 2>/dev/null)" || return 0
  if printf '{"state":%s,"step":%s,"message":%s,"ref":%s,"pid":%d,"at":%s}\n' \
      "$(json_string "$state")" "$(json_string "$STEP")" "$(json_or_null "$message")" \
      "$(json_or_null "$REF")" "$$" "$(json_string "$(date -u +%Y-%m-%dT%H:%M:%SZ)")" > "$tmp"; then
    mv -f -- "$tmp" "$STATUS_PATH" 2>/dev/null || rm -f -- "$tmp"
  else
    rm -f -- "$tmp"
  fi
}

step() {
  STEP="$1"
  log "$2"
  write_status "running" "$2"
}

die() {
  trap - ERR
  # A second Ctrl+C during the rollback must not stop it half-way.
  trap '' INT TERM HUP
  # Keep going if the window is already gone and printing fails: the rollback must finish.
  set +e
  rollback
  write_status "error" "$1"
  printf '  \033[31mx\033[0m %s\n' "$1" >&2
  exit 1
}

# --- shared with uninstall.sh: begin (a test checks that both copies match) ---

# A JSON string literal for $1: backslashes, quotes and control characters escaped.
json_string() {
  printf '"'
  printf '%s' "$1" | LC_ALL=C awk '
    BEGIN { ORS = ""; first = 1 }
    {
      gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); gsub(/\t/, "\\t"); gsub(/\r/, "\\r")
      gsub(/[\001-\010\013\014\016-\037\177]/, "")
      if (!first) printf "\\n"
      printf "%s", $0
      first = 0
    }'
  printf '"'
}

# The start time of process $1, or nothing when it does not run. Always in the
# C locale: a lock written from a shell with one LANG must read the same from
# a shell with another.
process_start() {
  LC_ALL=C ps -o lstart= -p "$1" 2>/dev/null | sed 's/^ *//; s/ *$//' || true
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
      "$LSOF" -nP -b -w -a -u "$(id -u)" -d cwd,txt -Fpfn 2>/dev/null | sed 's/^/L /' || true
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

# Whether lsof runs and can see working directories (it reports this script's
# own). Without it a running FlightRisk could go unnoticed, so callers refuse.
lsof_works() {
  [ -x "$LSOF" ] && [ -n "$("$LSOF" -nP -b -w -a -p "$$" -d cwd -Fn 2>/dev/null)" ]
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

# Take the install lock: a symbolic link, because creating one either succeeds
# or fails at once, and its target ("pid|start time") is set in the same step,
# so a lock never exists without its owner. A lock whose owner is gone (a run
# that was killed) is renamed aside before it is replaced, which only one of
# several runs can do; a run that finds it renamed a live lock gives it back.
acquire_lock() {
  local me aside
  mkdir -p -- "$FLIGHTRISK_HOME"
  me="$$|$(process_start "$$")"
  if ln -sn "$me" "$LOCK_LINK" 2>/dev/null; then
    LOCK_HELD=1
    return 0
  fi
  if lock_owner_alive "$LOCK_LINK"; then
    return 1
  fi
  aside="$LOCK_LINK.stale.$$"
  mv -- "$LOCK_LINK" "$aside" 2>/dev/null || return 1
  if lock_owner_alive "$aside"; then
    ln -sn "$(readlink "$aside")" "$LOCK_LINK" 2>/dev/null || true
    rm -f -- "$aside"
    return 1
  fi
  rm -f -- "$aside"
  ln -sn "$me" "$LOCK_LINK" 2>/dev/null || return 1
  LOCK_HELD=1
}

release_lock() {
  if [ "$LOCK_HELD" = "1" ]; then
    if [ "$(readlink "$LOCK_LINK" 2>/dev/null)" = "$$|$(process_start "$$")" ]; then
      rm -f -- "$LOCK_LINK"
    fi
    LOCK_HELD=0
  fi
}

# --- shared with uninstall.sh: end ---

# Remove first-download folders left by an earlier run that was interrupted.
# Called while holding the lock, so none of them belongs to a running install.
remove_stale_incoming() {
  local dir
  for dir in "$INCOMING_PREFIX"*; do
    if [ -d "$dir" ] && [ "$dir" != "$INCOMING_DIR" ]; then
      rm -rf -- "$dir"
      warn "removed $dir, left by an interrupted install"
    fi
  done
  # A package copy or a half-replaced package folder of an update that was
  # killed (the package stamp was removed first, so this run reinstalls), and
  # lock links renamed aside by a run that was killed in between.
  for dir in "$VENV_DIR.before-update" "$VENV_DIR.failed-update"; do
    if [ -d "$dir" ]; then rm -rf -- "$dir"; fi
  done
  for dir in "$LOCK_LINK".stale.*; do
    if [ -L "$dir" ]; then rm -f -- "$dir"; fi
  done
  return 0
}

# The newest release tag (vX.Y.Z only, so no pre-releases) among
# `git ls-remote --tags` lines on standard input; nothing when there is none.
pick_latest_tag() {
  awk '{ sub(/^refs\/tags\//, "", $2); print $2 }' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    | sed 's/^v//' \
    | sort -t. -k1,1n -k2,2n -k3,3n \
    | tail -n 1 \
    | sed 's/^\(.\)/v\1/' || true
}

valid_ref() {
  [ "$1" = "main" ] || [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

have() { command -v "$1" >/dev/null 2>&1; }

# The major.minor version of the Python at $1, or nothing if it does not run.
python_version() {
  "$1" -I -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true
}

# The Python 3.13 interpreters to try, one per line, in order of preference.
# FLIGHTRISK_PYTHON_CANDIDATES (colon separated) replaces the list.
python_candidates() {
  local found
  if [ -n "${FLIGHTRISK_PYTHON_CANDIDATES+x}" ]; then
    printf '%s\n' "$FLIGHTRISK_PYTHON_CANDIDATES" | tr ':' '\n'
    return 0
  fi
  # pyenv versions, newest patch release first.
  for found in "$HOME"/.pyenv/versions/3.13*/bin/python3.13; do
    [ -e "$found" ] && printf '%s\n' "$found"
  done | sort -r
  printf '%s\n' /opt/homebrew/opt/python@3.13/bin/python3.13 /usr/local/opt/python@3.13/bin/python3.13 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
  command -v python3.13 2>/dev/null || true
}

find_python() {
  local candidate
  if [ -n "${FLIGHTRISK_PYTHON:-}" ]; then
    if [ "$(python_version "$FLIGHTRISK_PYTHON")" = "$PYTHON_WANTED" ]; then
      PYTHON_BIN="$FLIGHTRISK_PYTHON"
      return 0
    fi
    die "FLIGHTRISK_PYTHON=$FLIGHTRISK_PYTHON is not a working Python $PYTHON_WANTED."
  fi
  while IFS= read -r candidate; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    if [ "$(python_version "$candidate")" = "$PYTHON_WANTED" ]; then
      PYTHON_BIN="$candidate"
      return 0
    fi
  done < <(python_candidates)
  return 1
}

# Homebrew is not on PATH in a fresh, non-login shell; look where it installs
# itself on Apple Silicon and on Intel Macs.
find_brew() {
  local candidate candidates
  have brew && return 0
  candidates="${FLIGHTRISK_BREW_CANDIDATES-/opt/homebrew/bin/brew:/usr/local/bin/brew}"
  while IFS= read -r candidate; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      eval "$("$candidate" shellenv)"
      return 0
    fi
  done < <(printf '%s\n' "$candidates" | tr ':' '\n')
  return 1
}

# Download the pinned Homebrew installer to a file, check the file's SHA-256 and
# only then run it. (Reading it into a variable with $(curl ...) would drop its
# final newline and change the checksum.)
install_homebrew() {
  local file sum
  if [ ! -t 0 ] && ! sudo -n true 2>/dev/null; then
    die "Homebrew is needed to install git or Python $PYTHON_WANTED, and installing it asks for your password, which this window cannot do. Run 'sudo -v' first and then this installer again, or install Homebrew from https://brew.sh."
  fi
  log "Installing Homebrew (it asks for your administrator password once)..."
  file="$(mktemp "${TMPDIR:-/tmp}/flightrisk-homebrew.XXXXXX")"
  if ! curl -fsSL -o "$file" "https://raw.githubusercontent.com/Homebrew/install/${HOMEBREW_INSTALL_COMMIT}/install.sh"; then
    rm -f -- "$file"
    die "Could not download the Homebrew installer. Check the network connection and run this again."
  fi
  sum="$(shasum -a 256 "$file" | cut -d' ' -f1)"
  if [ "$sum" != "$HOMEBREW_INSTALL_SHA256" ]; then
    rm -f -- "$file"
    die "The Homebrew installer did not match its expected checksum, so it was not run. Install Homebrew from https://brew.sh and run this again."
  fi
  if [ -t 0 ]; then
    /bin/bash "$file" || { rm -f -- "$file"; die "The Homebrew installer failed (see above)."; }
  else
    NONINTERACTIVE=1 /bin/bash "$file" || { rm -f -- "$file"; die "The Homebrew installer failed (see above)."; }
  fi
  rm -f -- "$file"
  find_brew || die "Homebrew was installed but brew was not found. Open a new Terminal window and run this again."
}

ensure_brew() {
  find_brew && return 0
  install_homebrew
}

ensure_git() {
  if have git && git --version >/dev/null 2>&1; then
    ok "git found ($(git --version))"
    return 0
  fi
  ensure_brew
  log "Installing git with Homebrew..."
  brew install git
  if ! { have git && git --version >/dev/null 2>&1; }; then
    die "git is still not available after installing it."
  fi
  ok "git installed"
}

ensure_python() {
  if find_python; then
    ok "Python $PYTHON_WANTED found ($PYTHON_BIN)"
    return 0
  fi
  ensure_brew
  log "Installing Python $PYTHON_WANTED with Homebrew..."
  brew install "python@$PYTHON_WANTED"
  find_python || die "Python $PYTHON_WANTED is still not available after installing it."
  ok "Python $PYTHON_WANTED installed ($PYTHON_BIN)"
}

# The release to install: FLIGHTRISK_REF, else the newest vX.Y.Z tag of the
# repository, else main.
select_ref() {
  local tags latest
  if [ -n "${FLIGHTRISK_REF:-}" ]; then
    valid_ref "$FLIGHTRISK_REF" || die "FLIGHTRISK_REF must be a release tag like v1.2.3, or main (got '$FLIGHTRISK_REF')."
    REF="$FLIGHTRISK_REF"
    return 0
  fi
  tags="$(git ls-remote --tags --refs "$REPO_URL" 2>/dev/null)" \
    || die "Could not reach $REPO_URL. Check the network connection and run this again."
  latest="$(printf '%s\n' "$tags" | pick_latest_tag)"
  if [ -n "$latest" ]; then
    REF="$latest"
  else
    REF="main"
    warn "no release has been published yet, so the newest code on main is installed"
  fi
}

refuse_if_running() {
  local who
  lsof_works || die "Could not check whether FlightRisk is running ($LSOF does not run), so nothing was changed."
  who="$(running_pids | describe_pids)"
  if [ -n "$who" ]; then
    die "FlightRisk is running from $APP_DIR (process $who). Land the drones, stop everything in the launcher window and quit it, then run this again. The installer never stops FlightRisk itself, because an edge may be flying a drone."
  fi
}

marker_path() { printf '%s/.git/%s' "${1:-$APP_DIR}" "$MARKER_NAME"; }

marker_value() {
  sed -n "s/^$1=//p" "$(marker_path)" 2>/dev/null | head -n 1 || true
}

# Write the marker for release $1 in the checkout at $2 (default APP_DIR). With
# $3, the marker also records an update to commit $3 that has started but not
# finished.
write_marker() {
  local ref="$1" dir="${2:-$APP_DIR}" pending="${3:-}" tmp
  tmp="$(marker_path "$dir").tmp"
  {
    printf 'installer=installer/macos/install.sh\nref=%s\ndate=%s\n' "$ref" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ -n "$pending" ]; then printf 'pending=%s\n' "$pending"; fi
  } > "$tmp"
  mv -f -- "$tmp" "$(marker_path "$dir")"
}

# Fetch $REF into the clone at $1 and set TARGET to the git object to check out.
fetch_ref() {
  local dir="$1"
  if [ "$REF" = "main" ]; then
    git -C "$dir" fetch --quiet --force origin "+refs/heads/main:refs/remotes/origin/main" \
      || die "Could not download the newest FlightRisk (git fetch failed)."
    TARGET="refs/remotes/origin/main"
  else
    git -C "$dir" fetch --quiet --force origin "+refs/tags/$REF:refs/tags/$REF" \
      || die "Could not download FlightRisk $REF (git fetch failed). Is it a published release?"
    TARGET="refs/tags/$REF"
  fi
}

# Check out $2 in the clone at $1. Never with --force, and with
# --no-overwrite-ignore, so a file of yours that a release starts to track
# (weights in models/, say) stops the update instead of being replaced.
# Half-written files of an interrupted update are reset before (see update_existing).
checkout() {
  git -C "$1" -c advice.detachedHead=false checkout --quiet --no-overwrite-ignore --detach "$2"
}

update_existing() {
  local head want pending
  [ -f "$(marker_path)" ] \
    || die "$APP_DIR was not made by this installer (your own checkout?), so it was not changed."
  refuse_if_running
  PREV_REF="$(marker_value ref)"
  pending="$(marker_value pending)"
  if [ -n "$pending" ]; then
    # Files this installer half-wrote; reset them to the commit HEAD names.
    warn "an earlier update was interrupted; restoring a clean checkout first"
    # git's own lock, left by the interrupted git; no other git runs here while we hold the install lock.
    rm -f -- "$APP_DIR/.git/index.lock"
    git -C "$APP_DIR" reset --quiet --hard HEAD
    write_marker "$PREV_REF"
  elif [ -n "$(git -C "$APP_DIR" status --porcelain --untracked-files=no)" ]; then
    die "$APP_DIR has local changes, so it was not changed. Save or undo them first."
  fi
  git -C "$APP_DIR" remote set-url origin "$REPO_URL"
  fetch_ref "$APP_DIR"
  head="$(git -C "$APP_DIR" rev-parse HEAD)"
  want="$(git -C "$APP_DIR" rev-parse "$TARGET^{commit}")"
  if [ "$head" = "$want" ]; then
    ok "already at $REF"
  elif git -C "$APP_DIR" merge-base --is-ancestor "$want" "$head" && [ "${FLIGHTRISK_ALLOW_DOWNGRADE:-}" != "1" ]; then
    warn "not moving to $REF: the installed version is newer (set FLIGHTRISK_ALLOW_DOWNGRADE=1 to go back)"
    REF="$PREV_REF"
    return 0
  else
    PREV_HEAD="$head"
    MOVED=1
    write_marker "$PREV_REF" "$APP_DIR" "$want"
    checkout "$APP_DIR" "$TARGET" || die "Could not switch $APP_DIR to $REF (git checkout failed). If git names a file of yours that $REF would replace, move it out of the folder and run this again."
    ok "moved to $REF"
  fi
  write_marker "$REF"
}

clone_or_update() {
  if [ -L "$APP_DIR" ]; then
    die "$APP_DIR is a link to another folder. Remove the link and run this again."
  fi
  if [ -d "$APP_DIR/.git" ]; then
    update_existing
    return 0
  fi
  if [ -e "$APP_DIR" ] && [ -n "$(ls -A -- "$APP_DIR" 2>/dev/null)" ]; then
    die "$APP_DIR exists but is not a FlightRisk install made by this installer, so it was not changed."
  fi
  # The first download goes to a folder of its own and is moved into place only
  # when it is complete, so an interrupted run never leaves a half-made install
  # where the app, the updater and the uninstaller look for one.
  log "Downloading FlightRisk $REF into $APP_DIR ..."
  mkdir -p -- "$FLIGHTRISK_HOME"
  INCOMING_DIR="$INCOMING_PREFIX$$"
  rm -rf -- "$INCOMING_DIR"
  git clone --quiet --no-checkout "$REPO_URL" "$INCOMING_DIR" \
    || die "git clone of $REPO_URL failed. Check the network connection and run this again."
  fetch_ref "$INCOMING_DIR"
  checkout "$INCOMING_DIR" "$TARGET" || die "Could not check out FlightRisk $REF. Is it a published release?"
  write_marker "$REF" "$INCOMING_DIR"
  if [ -e "$APP_DIR" ]; then rmdir -- "$APP_DIR"; fi
  mv -- "$INCOMING_DIR" "$APP_DIR"
  INCOMING_DIR=""
  ok "downloaded $REF"
}

# After a failure or an interruption: put an install that this run moved back
# on the release it had (code and packages), and remove a first download that
# did not finish.
rollback() {
  if [ "$MOVED" = "1" ] && [ -n "$PREV_HEAD" ]; then
    MOVED=0
    rm -f -- "$APP_DIR/.git/index.lock"
    git -C "$APP_DIR" reset --quiet --hard HEAD 2>/dev/null || true
    if checkout "$APP_DIR" "$PREV_HEAD" 2>/dev/null; then
      write_marker "$PREV_REF" 2>/dev/null || true
      warn "put $APP_DIR back on the version it had before ($PREV_HEAD); run the installer again to retry"
    fi
    if [ -n "$VENV_SNAPSHOT" ] && [ -d "$VENV_SNAPSHOT" ]; then
      # Swap, so that one complete copy exists at every moment.
      rm -rf -- "$VENV_DIR.failed-update"
      if mv -- "$VENV_DIR" "$VENV_DIR.failed-update" && mv -- "$VENV_SNAPSHOT" "$VENV_DIR"; then
        rm -rf -- "$VENV_DIR.failed-update"
        warn "put the packages of that version back"
      fi
    fi
  fi
  if [ -n "$VENV_SNAPSHOT" ] && [ -d "$VENV_SNAPSHOT" ]; then
    rm -rf -- "$VENV_SNAPSHOT"
  fi
  VENV_SNAPSHOT=""
  if [ -n "$INCOMING_DIR" ]; then
    rm -rf -- "$INCOMING_DIR"
    INCOMING_DIR=""
  fi
}

create_venv() {
  if [ -x "$VENV_DIR/bin/python" ] && [ "$(python_version "$VENV_DIR/bin/python")" = "$PYTHON_WANTED" ]; then
    ok "virtual environment found ($VENV_DIR)"
    return 0
  fi
  if [ -e "$VENV_DIR" ]; then
    warn "the virtual environment is not Python $PYTHON_WANTED; making a new one"
    rm -rf -- "$VENV_DIR"
  fi
  "$PYTHON_BIN" -m venv "$VENV_DIR" || die "Could not create the virtual environment in $VENV_DIR."
  ok "virtual environment created ($VENV_DIR)"
}

# The requirements file to install from: the pinned lock on Apple Silicon with
# macOS 14 or newer, else requirements.txt with its version floors only.
requirements_file() {
  local major
  major="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
  case "$major" in ''|*[!0-9]*) major=0 ;; esac
  if [ "$(uname -m)" = "arm64" ] && [ "$major" -ge 14 ] && [ -f "$APP_DIR/$LOCK_FILE_MACOS" ]; then
    printf '%s\n' "$LOCK_FILE_MACOS"
  else
    printf '%s\n' "requirements.txt"
  fi
}

install_dependencies() {
  local req stamp sum
  if [ "${FLIGHTRISK_INSTALL_SKIP_DEPS:-}" = "1" ]; then
    warn "FLIGHTRISK_INSTALL_SKIP_DEPS=1: no packages installed and no weights downloaded"
    return 0
  fi
  req="$(requirements_file)"
  stamp="$VENV_DIR/.flightrisk-deps"
  sum="$req $(shasum -a 256 "$APP_DIR/$req" | cut -d' ' -f1)"
  if [ -f "$stamp" ] && [ "$(cat -- "$stamp")" = "$sum" ]; then
    ok "packages already installed from $req"
    return 0
  fi
  # An update keeps a copy of the packages it is about to change (a clone on
  # APFS, so it costs no space until files differ), to put back if it fails.
  if [ "$MOVED" = "1" ] && [ -f "$stamp" ]; then
    # cp -c falls back to a plain copy where cloning is not possible, so a
    # failure here is a real one (a full disk): stop before anything changes.
    rm -rf -- "$VENV_DIR.before-update"
    if ! cp -cR -- "$VENV_DIR" "$VENV_DIR.before-update" 2>/dev/null; then
      rm -rf -- "$VENV_DIR.before-update"
      die "Could not keep a copy of the installed packages (is the disk full?), so the update was not made."
    fi
    VENV_SNAPSHOT="$VENV_DIR.before-update"
  fi
  # The stamp goes first: packages that are half installed must not count as installed.
  rm -f -- "$stamp"
  log "Installing the Python packages from $req (several GB; this takes a while)..."
  # --isolated: pip's configuration files and PIP_* variables (an extra index,
  # say) are ignored; the index is named explicitly.
  if [ "$req" = "$LOCK_FILE_MACOS" ]; then
    (cd "$APP_DIR" && "$VENV_DIR/bin/python" -I -m pip --isolated install --require-virtualenv --no-input \
      --index-url "$PIP_INDEX" --require-hashes --only-binary :all: -r "$req")
  else
    warn "no pinned package list for this Mac; installing requirements.txt (version floors only, no hashes)"
    (cd "$APP_DIR" && "$VENV_DIR/bin/python" -I -m pip --isolated install --require-virtualenv --no-input \
      --index-url "$PIP_INDEX" -r "$req")
  fi
  printf '%s\n' "$sum" > "$stamp"
  if [ -n "$VENV_SNAPSHOT" ]; then
    rm -rf -- "$VENV_SNAPSHOT"
    VENV_SNAPSHOT=""
  fi
  ok "packages installed"
}

sha256_of() {
  shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1
}

download_weights() {
  local target="$APP_DIR/yolo11n.pt" file
  if [ "${FLIGHTRISK_INSTALL_SKIP_DEPS:-}" = "1" ]; then
    return 0
  fi
  if [ -f "$target" ] && [ "$(sha256_of "$target")" = "$WEIGHTS_SHA256" ]; then
    ok "yolo11n.pt found"
  else
    file="$target.download.$$"
    if ! curl -fsSL --retry 2 -o "$file" "$WEIGHTS_URL"; then
      rm -f -- "$file"
      warn "could not download yolo11n.pt now; run the installer again before flying (Ultralytics would otherwise download it unchecked)"
    elif [ "$(sha256_of "$file")" != "$WEIGHTS_SHA256" ]; then
      rm -f -- "$file"
      die "The downloaded yolo11n.pt did not match its expected checksum, so it was not installed."
    else
      mv -f -- "$file" "$target"
      ok "yolo11n.pt downloaded and checked"
    fi
  fi
  if [ ! -f "$APP_DIR/models/heridal_yolo11s.pt" ]; then
    warn "optional: the fine-tuned top_down weights are not installed; copy heridal_yolo11s.pt into $APP_DIR/models/ if you have them"
  fi
}

# Whether $1 is an app bundle inside an Applications folder.
in_applications_folder() {
  case "$1" in
    /Applications/*|"$HOME"/Applications/*) return 0 ;;
    *) return 1 ;;
  esac
}

create_app() {
  if [ "${FLIGHTRISK_NO_APP:-}" = "1" ]; then
    warn "FLIGHTRISK_NO_APP=1: FlightRisk.app not created"
    return 0
  fi
  if [ -n "${FLIGHTRISK_CALLER_APP:-}" ] && in_applications_folder "$FLIGHTRISK_CALLER_APP"; then
    ok "using $FLIGHTRISK_CALLER_APP"
    APP_BUNDLE="$FLIGHTRISK_CALLER_APP"
    return 0
  fi
  if bash "$APP_DIR/installer/macos/make-app.sh" install --dest "$APPLICATIONS_DIR" --ref "$REF"; then
    APP_BUNDLE="$APPLICATIONS_DIR/FlightRisk.app"
    ok "created $APP_BUNDLE"
  else
    warn "FlightRisk.app was not created (see above); start the launcher with $APP_DIR/scripts/launcher.command instead"
  fi
}

on_error() {
  die "Failed at line $1: $2"
}

main() {
  APP_BUNDLE=""
  [ "$(uname -s)" = "Darwin" ] || { printf 'This installer is for macOS. On Windows use installer/windows/install.ps1.\n' >&2; exit 1; }
  if unsafe_home "$FLIGHTRISK_HOME"; then
    printf '  \033[31mx\033[0m FLIGHTRISK_HOME=%s is a shared folder; choose a folder of its own.\n' "$FLIGHTRISK_HOME" >&2
    exit 1
  fi
  mkdir -p -- "$FLIGHTRISK_HOME"
  if ! acquire_lock; then
    printf '  \033[31mx\033[0m %s\n' "Another FlightRisk install is running (pid $(readlink "$LOCK_LINK" 2>/dev/null | cut -d'|' -f1)). Wait for it to finish." >&2
    exit 1
  fi
  trap release_lock EXIT
  trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
  # A closed Terminal window sends HUP, Ctrl+C sends INT: undo what is half done.
  trap 'die "Interrupted; nothing half-done was left behind. Run the installer again to finish."' INT TERM HUP
  write_status "running" ""
  remove_stale_incoming

  log "FlightRisk -- macOS install into $FLIGHTRISK_HOME"
  ok "macOS $(sw_vers -productVersion 2>/dev/null || echo unknown) ($(uname -m))"
  [ "$(uname -m)" = "arm64" ] || warn "this Mac is not Apple Silicon; the installer is tested on Apple Silicon only"

  step prerequisites "Checking git and Python $PYTHON_WANTED"
  ensure_git
  ensure_python

  step download "Choosing the release"
  select_ref
  ok "release: $REF"
  clone_or_update

  step venv "Setting up the virtual environment"
  create_venv

  step packages "Installing the Python packages"
  install_dependencies

  step weights "Detector weights"
  download_weights

  step app "Creating FlightRisk.app"
  create_app

  MOVED=0
  STEP="done"
  write_status "done" ""
  log "Done. FlightRisk $REF is installed in $APP_DIR."
  if [ -n "$APP_BUNDLE" ]; then
    echo "  Open FlightRisk from $APP_BUNDLE (or Spotlight: FlightRisk)."
  fi
  echo "  The edge token is read from FLIGHTRISK_EDGE_TOKEN or ~/.flightrisk-fleet.env; this installer never asks for it."
  echo "  Update:    bash \"$APP_DIR/installer/macos/update.sh\""
  echo "  Uninstall: bash \"$APP_DIR/installer/macos/uninstall.sh\""
  if [ "${FLIGHTRISK_OPEN_WHEN_DONE:-}" = "1" ] && [ -n "$APP_BUNDLE" ]; then
    release_lock  # the app does not start the launcher while the lock is held
    open "$APP_BUNDLE" || true
  fi
}

# Tests source this file with FLIGHTRISK_INSTALL_LIB_ONLY=1 to call the
# functions above one at a time.
if [ "${FLIGHTRISK_INSTALL_LIB_ONLY:-}" = "1" ]; then
  # shellcheck disable=SC2317  # reached only when the file is run rather than sourced
  return 0 2>/dev/null || exit 0
fi
main "$@"
