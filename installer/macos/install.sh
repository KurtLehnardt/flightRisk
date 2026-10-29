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
#      OpenCV, MAVSDK and the rest).
#   4. Downloads the yolo11n detector weights. The fine-tuned HERIDAL weights
#      (models/heridal_yolo11s.pt) are not published; copy them into app/models
#      yourself if you have them.
#   5. Creates ~/Applications/FlightRisk.app, which starts the launcher window.
#
# It never asks for, stores or prints the edge token. The launcher reads it from
# FLIGHTRISK_EDGE_TOKEN or ~/.flightrisk-fleet.env, as before. ~/.flightrisk
# (presets and the saved ceiling) is not touched.
#
# Progress goes to a status file, by default
# ~/Library/Application Support/FlightRisk/install-status.json, as one JSON
# object: {"state": "running"|"done"|"error", "step": "...", "message": ...,
# "ref": ..., "pid": N, "at": "..."}. A lock folder next to it keeps two
# installs from running at once.
#
# Settings (environment variables, all optional):
#   FLIGHTRISK_REF               release tag (v1.2.3) or main; default: the newest release
#   FLIGHTRISK_HOME              install root; default ~/Library/Application Support/FlightRisk
#   FLIGHTRISK_APPLICATIONS_DIR  where FlightRisk.app goes; default ~/Applications
#   FLIGHTRISK_PYTHON            a Python 3.13 to build the virtual environment with
#   FLIGHTRISK_STATUS_FILE       where the status JSON goes
#   FLIGHTRISK_REPO_URL          the repository to install from (tests use a local one)
#   FLIGHTRISK_ALLOW_DOWNGRADE=1 allow moving an install to an older release
#   FLIGHTRISK_INSTALL_SKIP_DEPS=1  create the virtual environment but install no
#                                packages and download no weights (tests, dry runs)
#   FLIGHTRISK_NO_APP=1          do not create FlightRisk.app
#   FLIGHTRISK_OPEN_WHEN_DONE=1  open FlightRisk.app at the end
set -euo pipefail
set -o errtrace

FLIGHTRISK_HOME="${FLIGHTRISK_HOME:-$HOME/Library/Application Support/FlightRisk}"
APP_DIR="$FLIGHTRISK_HOME/app"
VENV_DIR="$APP_DIR/venv"
STATUS_PATH="${FLIGHTRISK_STATUS_FILE:-$FLIGHTRISK_HOME/install-status.json}"
LOCK_DIR="$FLIGHTRISK_HOME/install.lock.d"
REPO_URL="${FLIGHTRISK_REPO_URL:-https://github.com/KurtLehnardt/flightRisk.git}"
APPLICATIONS_DIR="${FLIGHTRISK_APPLICATIONS_DIR:-$HOME/Applications}"
MARKER_NAME="flightrisk-installer"
PYTHON_WANTED="3.13"
LOCK_FILE_MACOS="installer/locks/macos-arm64.txt"
# The Homebrew installer is fetched from a fixed commit and checked against this
# SHA-256 before it runs, so the bytes executed are known in advance. Update both
# together.
HOMEBREW_INSTALL_COMMIT="8ab1549dfa1189fd4d818a2116592d8f0ee06d8c"
HOMEBREW_INSTALL_SHA256="5f333bbe53bc490e51e7ccb1df8779b3dd6ee73a1a7379efda216edb08ccb148"

LOCK_HELD=0
STEP="start"
REF=""
PREV_HEAD=""
MOVED=0
CREATED_APP_DIR=0
PYTHON_BIN=""
TARGET=""

log()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mok\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

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
  rollback
  write_status "error" "$1"
  printf '  \033[31mx\033[0m %s\n' "$1" >&2
  exit 1
}

# Take the install lock: a folder, because mkdir either creates it or fails,
# atomically. The pid inside lets a later run recognise a lock left behind by
# an install that was killed, and take it over.
acquire_lock() {
  local other
  mkdir -p -- "$FLIGHTRISK_HOME"
  if ! mkdir -- "$LOCK_DIR" 2>/dev/null; then
    other="$(cat -- "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ -z "$other" ]; then
      sleep 1  # the other install may be between its mkdir and writing its pid
      other="$(cat -- "$LOCK_DIR/pid" 2>/dev/null || true)"
    fi
    if [[ "$other" =~ ^[0-9]+$ ]] && kill -0 "$other" 2>/dev/null; then
      return 1
    fi
    rm -rf -- "$LOCK_DIR"
    mkdir -- "$LOCK_DIR" 2>/dev/null || return 1
  fi
  printf '%s\n' "$$" > "$LOCK_DIR/pid"
  LOCK_HELD=1
}

release_lock() {
  if [ "$LOCK_HELD" = "1" ]; then
    rm -rf -- "$LOCK_DIR"
    LOCK_HELD=0
  fi
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
  "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true
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

install_homebrew() {
  local script sum
  if [ ! -t 0 ] && ! sudo -n true 2>/dev/null; then
    die "Homebrew is needed to install git or Python $PYTHON_WANTED, and installing it asks for your password, which this window cannot do. Run 'sudo -v' first and then this installer again, or install Homebrew from https://brew.sh."
  fi
  log "Installing Homebrew (it asks for your administrator password once)..."
  script="$(curl -fsSL "https://raw.githubusercontent.com/Homebrew/install/${HOMEBREW_INSTALL_COMMIT}/install.sh")" \
    || die "Could not download the Homebrew installer. Check the network connection and run this again."
  sum="$(printf '%s' "$script" | shasum -a 256 | cut -d' ' -f1)"
  [ "$sum" = "$HOMEBREW_INSTALL_SHA256" ] \
    || die "The Homebrew installer did not match its expected checksum, so it was not run. Install Homebrew from https://brew.sh and run this again."
  /bin/bash -c "$script"
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

# Process ids of programs running from this install's virtual environment
# (the launcher, a ground station, an edge), never our own.
running_pids() {
  ps -axww -o pid=,args= 2>/dev/null | awk -v venv="$VENV_DIR/" -v me="$$" '
    { pid = $1; line = $0; sub(/^[ \t]*[0-9]+[ \t]+/, "", line)
      if (pid != me && index(line, venv) == 1) print pid }'
}

refuse_if_running() {
  local pids
  pids="$(running_pids | tr '\n' ' ')"
  if [ -n "${pids// /}" ]; then
    die "FlightRisk is running from $APP_DIR (process ${pids% }). Land the drones, stop everything in the launcher window and quit it, then run this again. The installer never stops FlightRisk itself, because an edge may be flying a drone."
  fi
}

write_marker() {
  printf 'installer=installer/macos/install.sh\nref=%s\ndate=%s\n' "$REF" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    > "$APP_DIR/.git/$MARKER_NAME"
}

# Fetch $REF into the clone and set TARGET to the git object to check out.
fetch_ref() {
  if [ "$REF" = "main" ]; then
    git -C "$APP_DIR" fetch --quiet --force origin "+refs/heads/main:refs/remotes/origin/main" \
      || die "Could not download the newest FlightRisk (git fetch failed)."
    TARGET="refs/remotes/origin/main"
  else
    git -C "$APP_DIR" fetch --quiet --force origin "+refs/tags/$REF:refs/tags/$REF" \
      || die "Could not download FlightRisk $REF (git fetch failed). Is it a published release?"
    TARGET="refs/tags/$REF"
  fi
}

checkout() {
  git -C "$APP_DIR" -c advice.detachedHead=false checkout --quiet --detach "$1"
}

clone_or_update() {
  local head want
  if [ -L "$APP_DIR" ]; then
    die "$APP_DIR is a link to another folder. Remove the link and run this again."
  fi
  if [ -d "$APP_DIR/.git" ]; then
    [ -f "$APP_DIR/.git/$MARKER_NAME" ] \
      || die "$APP_DIR was not made by this installer (your own checkout?), so it was not changed."
    refuse_if_running
    if [ -n "$(git -C "$APP_DIR" status --porcelain --untracked-files=no)" ]; then
      die "$APP_DIR has local changes, so it was not changed. Save or undo them first."
    fi
    git -C "$APP_DIR" remote set-url origin "$REPO_URL"
    fetch_ref
    head="$(git -C "$APP_DIR" rev-parse HEAD)"
    want="$(git -C "$APP_DIR" rev-parse "$TARGET^{commit}")"
    if [ "$head" = "$want" ]; then
      ok "already at $REF"
    elif git -C "$APP_DIR" merge-base --is-ancestor "$want" "$head" && [ "${FLIGHTRISK_ALLOW_DOWNGRADE:-}" != "1" ]; then
      warn "not moving to $REF: the installed version is newer (set FLIGHTRISK_ALLOW_DOWNGRADE=1 to go back)"
      REF="$(sed -n 's/^ref=//p' "$APP_DIR/.git/$MARKER_NAME" | head -n 1)"
      return 0
    else
      PREV_HEAD="$head"
      MOVED=1
      checkout "$TARGET" || die "Could not switch $APP_DIR to $REF (git checkout failed)."
      ok "moved to $REF"
    fi
    write_marker
    return 0
  fi
  if [ -e "$APP_DIR" ] && [ -n "$(ls -A -- "$APP_DIR" 2>/dev/null)" ]; then
    die "$APP_DIR exists but is not a FlightRisk install made by this installer, so it was not changed."
  fi
  log "Downloading FlightRisk $REF into $APP_DIR ..."
  mkdir -p -- "$FLIGHTRISK_HOME"
  CREATED_APP_DIR=1
  git clone --quiet --no-checkout "$REPO_URL" "$APP_DIR" \
    || die "git clone of $REPO_URL failed. Check the network connection and run this again."
  fetch_ref
  checkout "$TARGET" || die "Could not check out FlightRisk $REF. Is it a published release?"
  write_marker
  CREATED_APP_DIR=0
  ok "downloaded $REF"
}

# After a failure: put an install that this run moved back where it was, and
# remove a folder this run created but could not finish.
rollback() {
  if [ "$MOVED" = "1" ] && [ -n "$PREV_HEAD" ]; then
    MOVED=0
    if checkout "$PREV_HEAD" 2>/dev/null; then
      warn "put $APP_DIR back on the version it had before ($PREV_HEAD); run the installer again to retry"
    fi
  fi
  if [ "$CREATED_APP_DIR" = "1" ]; then
    CREATED_APP_DIR=0
    rm -rf -- "$APP_DIR"
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
  if [ "$req" = "requirements.txt" ]; then
    warn "no pinned package list for this Mac; installing requirements.txt (version floors only)"
  fi
  stamp="$VENV_DIR/.flightrisk-deps"
  sum="$req $(shasum -a 256 "$APP_DIR/$req" | cut -d' ' -f1)"
  if [ -f "$stamp" ] && [ "$(cat -- "$stamp")" = "$sum" ]; then
    ok "packages already installed from $req"
    return 0
  fi
  log "Installing the Python packages from $req (several GB; this takes a while)..."
  "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
  (cd "$APP_DIR" && "$VENV_DIR/bin/python" -m pip install --require-virtualenv -r "$req")
  printf '%s\n' "$sum" > "$stamp"
  ok "packages installed"
}

download_weights() {
  if [ "${FLIGHTRISK_INSTALL_SKIP_DEPS:-}" = "1" ]; then
    return 0
  fi
  if [ -f "$APP_DIR/yolo11n.pt" ]; then
    ok "yolo11n.pt found"
  elif (cd "$APP_DIR" && "$VENV_DIR/bin/python" -c "from ultralytics import YOLO; YOLO('yolo11n.pt')" >/dev/null); then
    ok "yolo11n.pt downloaded"
  else
    warn "could not download yolo11n.pt now; Ultralytics downloads it the first time an edge starts"
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
  mkdir -p -- "$FLIGHTRISK_HOME"
  if ! acquire_lock; then
    printf '  \033[31mx\033[0m %s\n' "Another FlightRisk install is running (pid $(cat -- "$LOCK_DIR/pid" 2>/dev/null)). Wait for it to finish." >&2
    exit 1
  fi
  trap release_lock EXIT
  trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
  write_status "running" ""

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
