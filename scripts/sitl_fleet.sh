#!/usr/bin/env bash
# Native SITL fleet harness: start, probe, inspect and stop a fleet of ArduCopter or PX4 vehicles, one process
# per vehicle, and run the SITL tests against a freshly started fleet per test file.
#
# Usage:
#   scripts/sitl_fleet.sh up --autopilot ardupilot|px4 [--version V] [--count N] [--base-instance K]
#                            [--same-sysid] [--wait-ready] [--ready-timeout S] [--dry-run]
#   scripts/sitl_fleet.sh down [--base-instance K]
#   scripts/sitl_fleet.sh status [--base-instance K]
#   scripts/sitl_fleet.sh gate --autopilot ... [--version V] [--count N] [--base-instance K]
#                              [--files f1,f2] [--per-test] [--log FILE] [--dry-run]
#
# Versions: ArduCopter 4.6.3 or 4.7.3 (default 4.7.3), PX4 1.17.0. N is 1 to 8 (default 2), K defaults to 0.
# Vehicle i = K .. K+N-1 listens on tcpout://127.0.0.1:<5760+10i> (ArduCopter, system id i-K+1) or
# udpin://0.0.0.0:<14540+i> (PX4, system id i+1, set by PX4's own start script).
#
# Paths: FLIGHTRISK_SITL_HOME (default $HOME/sitl) holds bin/arducopter-<v>, params/copter-<v>.parm and
# PX4-Autopilot-<v>/build/px4_sitl_sih. Overrides: --ardupilot-bin, --ardupilot-defaults, --px4-build.
# The run directory (FLIGHTRISK_SITL_RUN_DIR, default $FLIGHTRISK_SITL_HOME/run-fleet-<K>) holds one directory
# per vehicle, pid files in pids/ and env.sh with the exports for the tests. PY selects the Python interpreter
# (default python3).
#
# The script never kills a process it did not start, never uses pkill by name, and never probes a TCP port with
# connect-and-close: ArduCopter SITL serves one client per serial port and does not boot until the first one
# connects, so a stray probe would consume that slot.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF="$ROOT/scripts/sitl_fleet.sh"
PY="${PY:-python3}"
SITL_HOME="${FLIGHTRISK_SITL_HOME:-$HOME/sitl}"

HOME_LAT="40.6894"
HOME_LON="-111.7310"
HOME_ALT="1600"
LISTEN_WAIT_S=30
START_WAIT_PX4_S=20
TERM_WAIT_S=10

die() { echo "sitl_fleet: $*" >&2; exit "${2:-1}"; }
usage_die() { echo "sitl_fleet: $*" >&2; echo "usage: $0 up|down|status|gate [options] (see the header of this script)" >&2; exit 2; }

# Defaults for every option, then one parser for all commands.
AUTOPILOT=""
VERSION=""
COUNT=2
BASE=0
SAME_SYSID=0
WAIT_READY=0
READY_TIMEOUT=180
DRY_RUN=0
ARDUPILOT_BIN=""
ARDUPILOT_DEFAULTS=""
PX4_BUILD=""
FILES=""
PER_TEST=0
LOG_FILE=""

is_uint() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

parse_options() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --autopilot) [ $# -ge 2 ] || usage_die "--autopilot needs a value"; AUTOPILOT="$2"; shift 2 ;;
            --version) [ $# -ge 2 ] || usage_die "--version needs a value"; VERSION="$2"; shift 2 ;;
            --count) [ $# -ge 2 ] || usage_die "--count needs a value"; COUNT="$2"; shift 2 ;;
            --base-instance) [ $# -ge 2 ] || usage_die "--base-instance needs a value"; BASE="$2"; shift 2 ;;
            --same-sysid) SAME_SYSID=1; shift ;;
            --wait-ready) WAIT_READY=1; shift ;;
            --ready-timeout) [ $# -ge 2 ] || usage_die "--ready-timeout needs a value"; READY_TIMEOUT="$2"; shift 2 ;;
            --dry-run) DRY_RUN=1; shift ;;
            --ardupilot-bin) [ $# -ge 2 ] || usage_die "--ardupilot-bin needs a value"; ARDUPILOT_BIN="$2"; shift 2 ;;
            --ardupilot-defaults) [ $# -ge 2 ] || usage_die "--ardupilot-defaults needs a value"; ARDUPILOT_DEFAULTS="$2"; shift 2 ;;
            --px4-build) [ $# -ge 2 ] || usage_die "--px4-build needs a value"; PX4_BUILD="$2"; shift 2 ;;
            --files) [ $# -ge 2 ] || usage_die "--files needs a value"; FILES="$2"; shift 2 ;;
            --per-test) PER_TEST=1; shift ;;
            --log) [ $# -ge 2 ] || usage_die "--log needs a value"; LOG_FILE="$2"; shift 2 ;;
            *) usage_die "unknown option: $1" ;;
        esac
    done
}

# Check the fleet shape; exits 2 on anything out of range. Sets VERSION's default.
validate_shape() {
    case "$AUTOPILOT" in
        ardupilot) [ -n "$VERSION" ] || VERSION="4.7.3"
            case "$VERSION" in 4.6.3|4.7.3) ;; *) usage_die "unknown ArduPilot version: $VERSION (use 4.6.3 or 4.7.3)" ;; esac ;;
        px4) [ -n "$VERSION" ] || VERSION="1.17.0"
            case "$VERSION" in 1.17.0) ;; *) usage_die "unknown PX4 version: $VERSION (use 1.17.0)" ;; esac ;;
        "") usage_die "--autopilot is required (ardupilot or px4)" ;;
        *) usage_die "unknown autopilot: $AUTOPILOT (use ardupilot or px4)" ;;
    esac
    is_uint "$COUNT" || usage_die "--count must be a number"
    [ "$COUNT" -ge 1 ] && [ "$COUNT" -le 8 ] || usage_die "--count must be 1 to 8"
    is_uint "$BASE" || usage_die "--base-instance must be a number"
    if [ "$AUTOPILOT" = px4 ] && [ $((BASE + COUNT)) -gt 10 ]; then
        usage_die "PX4 instances above 9 share UDP port 14549: base-instance plus count must be at most 10"
    fi
    if [ "$AUTOPILOT" = ardupilot ] && [ $((BASE + COUNT)) -gt 8 ]; then
        usage_die "ArduCopter ports run 5760 to 5830 (SITL-1): base-instance plus count must be at most 8"
    fi
    is_uint "$READY_TIMEOUT" || usage_die "--ready-timeout must be a whole number of seconds"
}

run_dir() { echo "${FLIGHTRISK_SITL_RUN_DIR:-$SITL_HOME/run-fleet-$BASE}"; }

# Per-instance helpers; $1 is the absolute instance number.
port_of() { if [ "$AUTOPILOT" = px4 ]; then echo $((14540 + $1)); else echo $((5760 + 10 * $1)); fi; }
address_of() {
    if [ "$AUTOPILOT" = px4 ]; then echo "udpin://0.0.0.0:$(port_of "$1")"; else echo "tcpout://127.0.0.1:$(port_of "$1")"; fi
}
sysid_of() {
    if [ "$AUTOPILOT" = px4 ]; then echo $(($1 + 1))
    elif [ "$SAME_SYSID" = 1 ]; then echo 1
    else echo $(($1 - BASE + 1)); fi
}
home_of() {
    awk -v lat="$HOME_LAT" -v lon="$HOME_LON" -v j=$(($1 - BASE)) -v alt="$HOME_ALT" \
        'BEGIN { printf "%s,%.4f,%s,0", lat, lon - 0.0001 * j, alt }'
}
name_of() { if [ "$AUTOPILOT" = px4 ]; then echo "p$1"; else echo "a$1"; fi; }

# Resolve binary and defaults paths for the chosen autopilot.
resolve_paths() {
    if [ "$AUTOPILOT" = ardupilot ]; then
        BIN="${ARDUPILOT_BIN:-$SITL_HOME/bin/arducopter-$VERSION}"
        BASE_DEFAULTS="${ARDUPILOT_DEFAULTS:-$SITL_HOME/params/copter-$VERSION.parm}"
        EXTRA_DEFAULTS="$ROOT/tests/integration/sitl/arducopter-${VERSION%.*}.parm"
    else
        PX4_DIR="${PX4_BUILD:-$SITL_HOME/PX4-Autopilot-$VERSION/build/px4_sitl_sih}"
        BIN="$PX4_DIR/bin/px4"
    fi
}

csv_of() {  # csv_of <function> -> comma list over the instances
    local f="$1" out="" i
    for ((i = BASE; i < BASE + COUNT; i++)); do out="${out:+$out,}$("$f" "$i")"; done
    echo "$out"
}

exports_text() {
    local addrs sysids first second
    addrs="$(csv_of address_of)"
    sysids="$(csv_of sysid_of)"
    first="$(address_of "$BASE")"
    echo "export FLIGHTRISK_SITL_ADDRESS=$first"
    if [ "$COUNT" -ge 2 ]; then
        second="$(address_of $((BASE + 1)))"
        echo "export FLIGHTRISK_SITL_ADDRESS_2=$second"
    fi
    echo "export FLIGHTRISK_SITL_ADDRESSES=$addrs"
    echo "export FLIGHTRISK_SITL_SYSIDS=$sysids"
    echo "export FLIGHTRISK_SITL_AUTOPILOT=$AUTOPILOT"
    echo "export FLIGHTRISK_SITL_VERSION=$VERSION"
}

pid_alive() { kill -0 "$1" 2>/dev/null; }

# The executable a pid file's process had when it was started, to avoid signalling a reused pid.
comm_of() { ps -p "$1" -o comm= 2>/dev/null | sed 's/^ *//'; }

# True when the run directory holds at least one live process we started.
live_pids_in() {
    local f pid
    for f in "$1"/pids/*.pid; do
        [ -e "$f" ] || continue
        pid="$(cat "$f" 2>/dev/null)"
        is_uint "$pid" && pid_alive "$pid" && return 0
    done
    return 1
}

# Each vehicle runs under a small supervisor shell that stays alive as its parent: an ArduCopter SITL process
# whose parent has exited dies as soon as its first client connects. The supervisor records its own pid and the
# vehicle's, waits for the vehicle and leaves the exit status next to the log.
SUPERVISOR='echo $$ > "$SUPFILE"; "$@" > "$LOGFILE" 2>&1 < /dev/null & v=$!; echo $v > "$PIDFILE"; wait $v; echo "exit $?" > "$(dirname "$LOGFILE")/exit-status"'

tcp_listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
udp_bound() { lsof -nP -iUDP:"$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- up

cmd_up() {
    parse_options "$@"
    validate_shape
    resolve_paths
    local rd i port name d pid comm n pidfile
    rd="$(run_dir)"

    if [ "$DRY_RUN" = 1 ]; then
        echo "SITL fleet plan (dry run, nothing started): autopilot=$AUTOPILOT version=$VERSION count=$COUNT base=$BASE"
        echo "run dir: $rd"
        echo "binary: $BIN"
        if [ "$AUTOPILOT" = ardupilot ]; then
            echo "defaults: $BASE_DEFAULTS"
            echo "extra defaults: $EXTRA_DEFAULTS"
        fi
        for ((i = BASE; i < BASE + COUNT; i++)); do
            echo "instance $i: address=$(address_of "$i") port=$(port_of "$i") sysid=$(sysid_of "$i") home=$(home_of "$i")"
        done
        exports_text
        return 0
    fi

    # Refusals: nothing is started and nothing of another run is touched.
    if live_pids_in "$rd"; then
        die "run directory $rd is busy: a fleet started from it is still running (use down --base-instance $BASE)"
    fi
    [ -x "$BIN" ] || die "missing binary: $BIN"
    if [ "$AUTOPILOT" = ardupilot ]; then
        [ -f "$BASE_DEFAULTS" ] || die "missing defaults file: $BASE_DEFAULTS"
        [ -f "$EXTRA_DEFAULTS" ] || die "missing defaults file: $EXTRA_DEFAULTS"
    fi
    command -v lsof >/dev/null 2>&1 || die "lsof is required to check ports"
    for ((i = BASE; i < BASE + COUNT; i++)); do
        port="$(port_of "$i")"
        if [ "$AUTOPILOT" = px4 ]; then
            udp_bound "$port" && die "UDP port $port is already bound by another process"
        else
            tcp_listening "$port" && die "TCP port $port is already listening (another SITL vehicle?)"
        fi
    done

    mkdir -p "$rd/pids" || die "cannot create $rd"
    rm -f "$rd"/pids/*.pid "$rd"/pids/*.comm "$rd"/pids/*.sup
    for ((i = BASE; i < BASE + COUNT; i++)); do
        name="$(name_of "$i")"
        d="$rd/$name"
        rm -rf "$d"
        mkdir -p "$d"
        pidfile="$rd/pids/$name.pid"
        if [ "$AUTOPILOT" = ardupilot ]; then
            logname=sitl.log
            set -- "$BIN" -w --model + --speedup 1 -I"$i" --sysid "$(sysid_of "$i")" \
                --defaults "$BASE_DEFAULTS,$EXTRA_DEFAULTS" --home "$(home_of "$i")"
            ( cd "$d" && PIDFILE="$pidfile" SUPFILE="$rd/pids/$name.sup" LOGFILE="$d/$logname" \
                nohup sh -c "$SUPERVISOR" sh "$@" ) > /dev/null 2>&1 < /dev/null &
        else
            logname=px4.log
            set -- "$BIN" -i "$i" -d "$PX4_DIR/etc" -w "$d" -s etc/init.d-posix/rcS
            ( cd "$d" && PIDFILE="$pidfile" SUPFILE="$rd/pids/$name.sup" LOGFILE="$d/$logname" \
                PX4_SYS_AUTOSTART=10040 PX4_HOME_LAT="$HOME_LAT" \
                PX4_HOME_LON="$(home_of "$i" | cut -d, -f2)" PX4_HOME_ALT="$HOME_ALT" \
                nohup sh -c "$SUPERVISOR" sh "$@" ) > /dev/null 2>&1 < /dev/null &
        fi
    done
    # The supervisors write the vehicle pid files; give them a moment.
    for ((i = BASE; i < BASE + COUNT; i++)); do
        name="$(name_of "$i")"
        n=0
        until [ -s "$rd/pids/$name.pid" ]; do
            n=$((n + 1))
            [ "$n" -gt 50 ] && { stop_fleet "$rd" >/dev/null; die "$name did not start"; }
            sleep 0.1
        done
    done

    # Wait for the vehicles to come up. ArduCopter: LISTEN on its port (lsof only, never a connect).
    for ((i = BASE; i < BASE + COUNT; i++)); do
        name="$(name_of "$i")"
        pid="$(cat "$rd/pids/$name.pid")"
        n=0
        if [ "$AUTOPILOT" = ardupilot ]; then
            port="$(port_of "$i")"
            until tcp_listening "$port"; do
                pid_alive "$pid" || { stop_fleet "$rd" >/dev/null; die "$name exited during start (see $rd/$name/sitl.log)"; }
                n=$((n + 1))
                if [ "$n" -gt "$LISTEN_WAIT_S" ]; then
                    stop_fleet "$rd" >/dev/null
                    die "TCP port $port never listened (see $rd/$name/sitl.log)"
                fi
                sleep 1
            done
        else
            # PX4 binds nothing on the vehicle port; it logs "Ready for takeoff" once its estimator is up.
            until grep -q "Ready for takeoff" "$rd/$name/px4.log" 2>/dev/null; do
                pid_alive "$pid" || { stop_fleet "$rd" >/dev/null; die "$name exited during start (see $rd/$name/px4.log)"; }
                n=$((n + 1))
                [ "$n" -gt "$START_WAIT_PX4_S" ] && break
                sleep 1
            done
        fi
        comm="$(comm_of "$pid")"
        [ -n "$comm" ] && echo "$comm" > "$rd/pids/$name.comm"
    done

    exports_text > "$rd/env.sh"
    echo "SITL fleet up: autopilot=$AUTOPILOT version=$VERSION count=$COUNT base=$BASE run dir $rd"
    cat "$rd/env.sh"

    if [ "$WAIT_READY" = 1 ]; then
        local args=() a
        for ((i = BASE; i < BASE + COUNT; i++)); do args+=(--address "$(address_of "$i")"); done
        ( cd "$ROOT" && "$PY" -m tests.integration.sitl_support wait-ready --timeout "$READY_TIMEOUT" "${args[@]}" )
        local rc=$?
        if [ "$rc" -ne 0 ]; then
            echo "sitl_fleet: not every vehicle became ready; the fleet is still running (down --base-instance $BASE stops it)" >&2
            return 1
        fi
    fi
    return 0
}

# ---------------------------------------------------------------- down

# Stop every process listed in the run directory's pid files; prints nothing, returns 1 if one survives.
stop_fleet() {
    local rd="$1" f pid want have n alive=0 pids=()
    for f in "$rd"/pids/*.pid; do
        [ -e "$f" ] || continue
        pid="$(cat "$f" 2>/dev/null)"
        is_uint "$pid" || continue
        pid_alive "$pid" || continue
        # A recycled pid belongs to somebody else: only signal the executable we recorded.
        want="$(cat "${f%.pid}.comm" 2>/dev/null)"
        have="$(comm_of "$pid")"
        if [ -n "$want" ] && [ "$want" != "$have" ]; then
            echo "sitl_fleet: pid $pid is no longer the recorded process; not signalling it" >&2
            continue
        fi
        pids+=("$pid")
    done
    if [ "${#pids[@]}" -gt 0 ]; then
        for pid in "${pids[@]}"; do kill -TERM "$pid" 2>/dev/null; done
        n=0
        while [ "$n" -lt "$TERM_WAIT_S" ]; do
            alive=0
            for pid in "${pids[@]}"; do pid_alive "$pid" && alive=1; done
            [ "$alive" = 0 ] && break
            sleep 1
            n=$((n + 1))
        done
        for pid in "${pids[@]}"; do pid_alive "$pid" && kill -KILL "$pid" 2>/dev/null; done
        sleep 1
        for pid in "${pids[@]}"; do
            if pid_alive "$pid"; then echo "sitl_fleet: pid $pid survived SIGKILL" >&2; return 1; fi
        done
    fi
    for f in "$rd"/pids/*.sup; do
        [ -e "$f" ] || continue
        pid="$(cat "$f" 2>/dev/null)"
        is_uint "$pid" && pid_alive "$pid" || continue
        case "$(comm_of "$pid")" in sh|*/sh) kill -TERM "$pid" 2>/dev/null ;; esac
    done
    rm -f "$rd"/pids/*.pid "$rd"/pids/*.comm "$rd"/pids/*.sup
    return 0
}

cmd_down() {
    parse_options "$@"
    local rd
    rd="$(run_dir)"
    if stop_fleet "$rd"; then
        echo "SITL fleet down"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------- status

cmd_status() {
    parse_options "$@"
    local rd f name pid state idx kind found=0
    rd="$(run_dir)"
    for f in "$rd"/pids/*.pid; do
        [ -e "$f" ] || continue
        found=1
        name="$(basename "$f" .pid)"
        idx="${name#?}"
        kind="${name%"$idx"}"
        pid="$(cat "$f" 2>/dev/null)"
        if is_uint "$pid" && pid_alive "$pid"; then state=running; else state=stopped; fi
        if [ "$kind" = p ]; then AUTOPILOT=px4; else AUTOPILOT=ardupilot; fi
        echo "$idx pid=$pid $state $(address_of "$idx")"
    done
    [ "$found" = 1 ] || echo "no fleet recorded in $rd"
    return 0
}

# ---------------------------------------------------------------- gate

gate_files() {
    local f
    if [ -n "$FILES" ]; then
        echo "$FILES" | tr ',' '\n'
    else
        for f in "$ROOT"/tests/integration/test_sitl*.py; do
            [ -e "$f" ] && echo "tests/integration/$(basename "$f")"
        done | sort
    fi
}

# Common options to pass on to a nested up/down.
fleet_args() {
    local a="--autopilot $AUTOPILOT --version $VERSION --count $COUNT --base-instance $BASE"
    [ "$SAME_SYSID" = 1 ] && a="$a --same-sysid"
    echo "$a"
}

gate_one() {  # gate_one <label> <pytest target>; returns the pytest exit status
    local label="$1" target="$2" rc env_sh
    echo "GATE start: $label"
    # shellcheck disable=SC2046
    if ! "$SELF" up $(fleet_args) --wait-ready --ready-timeout "$READY_TIMEOUT" \
            ${ARDUPILOT_BIN:+--ardupilot-bin "$ARDUPILOT_BIN"} \
            ${ARDUPILOT_DEFAULTS:+--ardupilot-defaults "$ARDUPILOT_DEFAULTS"} \
            ${PX4_BUILD:+--px4-build "$PX4_BUILD"}; then
        echo "sitl_fleet: fleet for $label did not come up"
        "$SELF" down --base-instance "$BASE"
        return 1
    fi
    env_sh="$(run_dir)/env.sh"
    # env.sh holds `export` lines: source it in a subshell so the tests see the variables.
    ( set -a; . "$env_sh"; cd "$ROOT" && exec "$PY" -m pytest "$target" -m sitl -s -p no:cacheprovider )
    rc=$?
    "$SELF" down --base-instance "$BASE"
    return "$rc"
}

gate_run() {
    local f node rc worst=0 results=() line targets target label
    trap '"$SELF" down --base-instance "$BASE" >/dev/null 2>&1' EXIT
    trap 'exit 130' INT TERM
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        if [ "$PER_TEST" = 1 ]; then
            targets="$( cd "$ROOT" && "$PY" -m pytest "$f" -m sitl --collect-only -q -p no:cacheprovider 2>/dev/null | grep '::' )"
            while IFS= read -r node; do
                [ -n "$node" ] || continue
                gate_one "$node" "$node"; rc=$?
                results+=("GATE $node: exit $rc")
                [ "$rc" -ne 0 ] && worst=1
            done <<EOF
$targets
EOF
        else
            gate_one "$f" "$f"; rc=$?
            results+=("GATE $f: exit $rc")
            [ "$rc" -ne 0 ] && worst=1
        fi
    done < <(gate_files)
    if [ "${#results[@]}" -eq 0 ]; then
        echo "sitl_fleet: no SITL test files found"
        worst=1
    else
        for line in "${results[@]}"; do echo "$line"; done
    fi
    echo "SITL GATE exit $worst"
    return "$worst"
}

cmd_gate() {
    parse_options "$@"
    validate_shape
    if [ "$DRY_RUN" = 1 ]; then
        local f
        echo "SITL gate plan (dry run, nothing started): autopilot=$AUTOPILOT version=$VERSION count=$COUNT base=$BASE per-test=$PER_TEST"
        while IFS= read -r f; do [ -n "$f" ] && echo "GATE would run: $f"; done < <(gate_files)
        return 0
    fi
    if [ -n "$LOG_FILE" ]; then
        mkdir -p "$(dirname "$LOG_FILE")"
        gate_run 2>&1 | tee -a "$LOG_FILE"
        return "${PIPESTATUS[0]}"
    fi
    gate_run
}

# ---------------------------------------------------------------- main

[ $# -ge 1 ] || usage_die "a command is required"
CMD="$1"
shift
case "$CMD" in
    up) cmd_up "$@"; exit $? ;;
    down) cmd_down "$@"; exit $? ;;
    status) cmd_status "$@"; exit $? ;;
    gate) cmd_gate "$@"; exit $? ;;
    -h|--help|help) sed -n '2,25p' "$0"; exit 0 ;;
    *) usage_die "unknown command: $CMD" ;;
esac
