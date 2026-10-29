#!/bin/bash
# The program inside FlightRisk.app (Contents/MacOS/FlightRisk).
#
# Opening the app does one of three things:
#
#   * FlightRisk is not installed yet (the app was dragged out of the .dmg):
#     it asks whether to install, then runs the installer that ships inside the
#     app (Contents/Resources/install.sh) in a Terminal window, where the
#     progress is shown. The installer opens the app again when it is done.
#   * The launcher this app started earlier is still running: it opens the
#     launcher window again.
#   * Otherwise it starts the launcher (python -m flightrisk.launcher) from the
#     install's virtual environment in the background and opens its window,
#     in a Chrome or Edge app window when one of those is installed (no tabs or
#     address bar), else in the default browser.
#
# The launcher keeps running after its window is closed; "Quit launcher" in the
# window ends it. Its output goes to ~/Library/Logs/FlightRisk/launcher.log.
# That file and the saved window address hold the launcher's per-launch key
# (not the edge token), so both are readable by this user only.
#
# Settings (environment variables; Finder passes none, so the defaults apply):
#   FLIGHTRISK_HOME             install root; default ~/Library/Application Support/FlightRisk
#   FLIGHTRISK_LOG_DIR          default ~/Library/Logs/FlightRisk
#   FLIGHTRISK_LAUNCHER_PORT    the launcher window's port; default 5500
#   FLIGHTRISK_OPEN_IN          window (default), browser, or none
#   FLIGHTRISK_APP_NONINTERACTIVE=1  show no dialogs (tests)
set -u
# This program's own files (the logs and the saved address) are private; the
# launcher itself is started with the umask the app was opened with.
ORIGINAL_UMASK="$(umask)"
umask 077

BUNDLE="$(cd -- "$(dirname -- "$0")/../.." && pwd -P)"
FLIGHTRISK_HOME="${FLIGHTRISK_HOME:-$HOME/Library/Application Support/FlightRisk}"
APP_DIR="$FLIGHTRISK_HOME/app"
PYTHON="$APP_DIR/venv/bin/python"
RUN_DIR="$FLIGHTRISK_HOME/run"
LOG_DIR="${FLIGHTRISK_LOG_DIR:-$HOME/Library/Logs/FlightRisk}"
PORT="${FLIGHTRISK_LAUNCHER_PORT:-5500}"
OPEN_IN="${FLIGHTRISK_OPEN_IN:-window}"
URL_FILE="$RUN_DIR/launcher.url"
PID_FILE="$RUN_DIR/launcher.pid"
LAUNCHER_LOG="$LOG_DIR/launcher.log"
START_TIMEOUT="${FLIGHTRISK_APP_START_TIMEOUT:-60}"

mkdir -p -- "$LOG_DIR" "$RUN_DIR" 2>/dev/null

note() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_DIR/app.log" 2>/dev/null || true
}

interactive() {
  [ "${FLIGHTRISK_APP_NONINTERACTIVE:-}" != "1" ]
}

# An alert with an OK button. $1 is the title, $2 the message.
alert() {
  note "$1: $2"
  interactive || return 0
  osascript - "$1" "$2" >/dev/null 2>&1 <<'APPLESCRIPT' || true
on run argv
  display alert (item 1 of argv) message (item 2 of argv) as critical
end run
APPLESCRIPT
}

# Ask a yes/no question; succeeds when the first button ($3) is chosen.
ask() {
  local answer
  interactive || return 1
  answer="$(osascript - "$1" "$2" "$3" <<'APPLESCRIPT' 2>/dev/null
on run argv
  set reply to display dialog (item 2 of argv) with title (item 1 of argv) buttons {"Cancel", item 3 of argv} default button 2 cancel button 1 with icon note
  return button returned of reply
end run
APPLESCRIPT
)" || return 1
  [ "$answer" = "$3" ]
}

installed() {
  [ -x "$PYTHON" ] && [ -f "$APP_DIR/.git/flightrisk-installer" ] && [ -f "$APP_DIR/flightrisk/launcher/__main__.py" ]
}

# Whether the launcher recorded in PID_FILE still runs.
launcher_alive() {
  local pid
  pid="$(cat -- "$PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  ps -ww -o args= -p "$pid" 2>/dev/null | grep -q -- "-m flightrisk.launcher"
}

# Open $1 in its own window (Chrome or Edge app mode), a browser tab, or not at all.
open_url() {
  local url="$1" browser
  case "$OPEN_IN" in
    none) note "window address ready (FLIGHTRISK_OPEN_IN=none)"; return 0 ;;
    browser) open "$url"; return $? ;;
  esac
  for browser in "/Applications/Google Chrome.app" "$HOME/Applications/Google Chrome.app" \
                 "/Applications/Microsoft Edge.app" "$HOME/Applications/Microsoft Edge.app"; do
    if [ -d "$browser" ]; then
      open -na "$browser" --args "--app=$url" && return 0
    fi
  done
  open "$url"
}

offer_install() {
  local script ref_file command_file lock_pid
  lock_pid="$(cat -- "$FLIGHTRISK_HOME/install.lock.d/pid" 2>/dev/null || true)"
  if [[ "$lock_pid" =~ ^[0-9]+$ ]] && kill -0 "$lock_pid" 2>/dev/null; then
    alert "FlightRisk is being installed" "An install is already running in a Terminal window. FlightRisk opens when it is done."
    exit 0
  fi
  script="$BUNDLE/Contents/Resources/install.sh"
  if [ ! -f "$script" ]; then
    alert "FlightRisk is not installed" "Install it with installer/macos/install.sh from the FlightRisk repository."
    exit 3
  fi
  if ! ask "Install FlightRisk" "FlightRisk is not installed on this Mac yet. Install it now?

It downloads FlightRisk and its Python packages (several GB) into your Library folder, in a Terminal window that shows the progress. Homebrew, git and Python 3.13 are installed first if they are missing." "Install"; then
    note "not installed; the install was not started"
    exit 3
  fi
  # A copy of the installer, so that ejecting the .dmg does not cut off a run.
  cp -f -- "$script" "$RUN_DIR/install.sh"
  ref_file="$BUNDLE/Contents/Resources/install-ref"
  command_file="$RUN_DIR/install-flightrisk.command"
  {
    printf '#!/bin/bash\n'
    printf 'export FLIGHTRISK_HOME=%q\n' "$FLIGHTRISK_HOME"
    printf 'export FLIGHTRISK_CALLER_APP=%q\n' "$BUNDLE"
    printf 'export FLIGHTRISK_OPEN_WHEN_DONE=1\n'
    if [ -z "${FLIGHTRISK_REF:-}" ] && [ -f "$ref_file" ]; then
      printf 'export FLIGHTRISK_REF=%q\n' "$(head -n 1 "$ref_file")"
    fi
    printf '/bin/bash %q\n' "$RUN_DIR/install.sh"
    cat <<'TAIL'
status=$?
if [ "$status" -ne 0 ]; then read -r -p "The install stopped (exit code $status). Press Enter to close this window. " _; fi
exit "$status"
TAIL
  } > "$command_file"
  chmod 700 "$command_file"
  note "starting the installer in Terminal"
  open -a Terminal "$command_file"
  exit 0
}

start_launcher() {
  local pid url deadline
  rm -f -- "$URL_FILE" "$PID_FILE"
  if [ -f "$LAUNCHER_LOG" ]; then mv -f -- "$LAUNCHER_LOG" "$LOG_DIR/launcher.previous.log"; fi
  : > "$LAUNCHER_LOG"
  note "starting the launcher on port $PORT"
  (
    umask "$ORIGINAL_UMASK"
    cd -- "$APP_DIR" || exit 1
    exec nohup "$PYTHON" -m flightrisk.launcher --port "$PORT" --no-browser >> "$LAUNCHER_LOG" 2>&1 < /dev/null
  ) &
  pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"
  deadline=$(( $(date +%s) + START_TIMEOUT ))
  url=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    url="$(sed -n 's/^FlightRisk launcher: \(http:\/\/127\.0\.0\.1:[0-9]*\/?key=[A-Za-z0-9_-]*\).*$/\1/p' "$LAUNCHER_LOG" | head -n 1)"
    [ -n "$url" ] && break
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    sleep 0.2
  done
  if [ -z "$url" ]; then
    rm -f -- "$PID_FILE"
    alert "The FlightRisk launcher did not start" "$(tail -n 5 "$LAUNCHER_LOG" 2>/dev/null)

The full output is in $LAUNCHER_LOG."
    exit 1
  fi
  printf '%s\n' "$url" > "$URL_FILE"
  note "launcher running (pid $pid)"
  open_url "$url"
}

if ! installed; then
  offer_install
fi
if launcher_alive && [ -s "$URL_FILE" ]; then
  note "the launcher is already running; opening its window"
  open_url "$(head -n 1 "$URL_FILE")"
  exit 0
fi
start_launcher
