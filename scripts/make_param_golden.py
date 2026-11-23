#!/usr/bin/env python3
"""Generate tests/golden/flight_params.json once, from parameters.md and the live code.

The golden file is the independent reference for the parameter registry. Its table cells come from
``docs/design/hardware-profiles/parameters.md`` (section 1 rows, section 1.1 constraints). Every value
that has a constant in today's code is checked against that constant while generating, by importing the
real module, and the constant is recorded as ``today_ref`` so a test can check it again later. The
accepted ranges (``today_accepts``) are read from the imported range constants and never typed in.

The script refuses to overwrite an existing output file (exit code 2). There is deliberately no
force flag. To regenerate after the base moved, delete the file by hand and say so in the pull request.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PARAMS_MD = ROOT / "docs" / "design" / "hardware-profiles" / "parameters.md"
DEFAULT_OUT = ROOT / "tests" / "golden" / "flight_params.json"
PROFILES = ("tello", "generic_multirotor")
SAFETY_SWITCH_IDS = {"sensing.mode", "sensing.turn", "link.require_failsafes", "tello.temp_refuse_c"}

SAF = "flightrisk.drone.safety"
FG = "flightrisk.drone.flight_guards"
OBS = "flightrisk.drone.obstacle_sensor"
TMP = "flightrisk.drone.temp_preflight"
TEL = "flightrisk.drone.tello"
TB = "flightrisk.drone.tello_bound"
MF = "flightrisk.drone.mavlink_fleet"
AP = "flightrisk.drone.autopilot"
CO = "flightrisk.drone.coordinator"
MC = "flightrisk.dashboard.manual_control"
DS = "flightrisk.dashboard.safety"
LP = "flightrisk.drone.lane_plan"
LF = "flightrisk.drone.lane_flight"
FS = "flightrisk.drone.fleet_scale"
FPC = "flightrisk.drone.fleet_plan_check"
FP = "flightrisk.drone.fleet_patterns"
PP = "flightrisk.drone.plan_paths"
OPT = MF + ":MavlinkFleetOptions."
# Modules read without importing them (the dashboard app pulls in Flask; scripts are not a package).
APP = "ast:flightrisk/dashboard/app.py:"
GROUND = "ast:scripts/field_fleet_ground.py:"

# Row id -> (tello ref, generic ref). A ref is "module:dotted.name", optionally "[i]" for a tuple item
# and "*scale" to convert metres to centimetres. "module:callable(param)" reads a parameter default;
# "ast:path:NAME" reads a module-level literal, and "ast:path:--flag" an argparse default, without
# importing the file; "~code=registry" renames an enum value; " & " joins refs into a list.
# None means no single runtime source, and the entry must then be listed in DOC_ONLY_TODAY.
TODAY_REFS = {
    "envelope.geofence_cm": (SAF + ":DEFAULT_GEOFENCE_CM", SAF + ":FIELD_LIMITS.default_geofence_cm"),
    "envelope.ceiling_cm": (SAF + ":DEFAULT_CEILING_CM", SAF + ":FIELD_LIMITS.default_ceiling_cm"),
    "envelope.ceiling_margin_cm": (SAF + ":DEFAULT_CEILING_MARGIN_CM", SAF + ":FIELD_LIMITS.default_ceiling_margin_cm"),
    "envelope.alt_slot_cm": (SAF + ":DEFAULT_ALT_SLOT_CM", SAF + ":FIELD_LIMITS.default_alt_slot_cm"),
    "move.min_cm": (SAF + ":MIN_MOVE_CM", SAF + ":FIELD_LIMITS.min_move_cm"),
    "move.max_cm": (SAF + ":MAX_MOVE_CM", SAF + ":FIELD_LIMITS.max_move_cm"),
    "envelope.max_geofence_cm": (None, SAF + ":FIELD_LIMITS.max_geofence_cm"),
    "envelope.fence_breach_margin_cm": (None, OPT + "fence_breach_margin_cm"),
    "envelope.autopilot_fence_margin_cm": (None, AP + ":FENCE_MARGIN_M*100"),
    "battery.min_takeoff_pct": (SAF + ":DEFAULT_MIN_TAKEOFF_BATTERY", SAF + ":DEFAULT_MIN_TAKEOFF_BATTERY"),
    "battery.critical_pct": (SAF + ":DEFAULT_LAND_BATTERY", SAF + ":DEFAULT_LAND_BATTERY"),
    "battery.rth_min_pct": (None, DS + ":DEFAULT_RTH_MIN_BATTERY_PCT"),
    "link.loss_detect_s": (SAF + ":DEFAULT_LINK_LOSS_LAND_S", SAF + ":DEFAULT_LINK_LOSS_LAND_S"),
    "link.loss_wait_s": (None, SAF + ":DEFAULT_FIELD_LINK_LOSS_HOLD_S"),
    "link.require_failsafes": (None, OPT + "require_failsafes"),
    "link.drone_stale_s": (None, OPT + "stale_after_s"),
    "link.rtl_accept_s": (None, OPT + "rtl_accept_s"),
    "gps.min_satellites": (None, OPT + "min_satellites"),
    "stranded.countdown_s": (MC + ":DEFAULT_ORPHAN_LAND_S", MC + ":DEFAULT_ORPHAN_LAND_S"),
    "takeoff.reply_timeout_s": (TEL + ":_TAKEOFF_REPLY_TIMEOUT_S", OPT + "takeoff_timeout_s"),
    "takeoff.late_s": (FG + ":DEFAULT_TAKEOFF_LATE_S", None),
    "takeoff.settle_samples": (FG + ":DEFAULT_SETTLE_SAMPLES", None),
    "takeoff.settle_tolerance_cm": (FG + ":DEFAULT_SETTLE_TOLERANCE_CM", None),
    "takeoff.settle_timeout_s": (FG + ":DEFAULT_SETTLE_TIMEOUT_S", None),
    "takeoff.refusal_retries": (FG + ":DEFAULT_REFUSAL_RETRIES", None),
    "takeoff.refusal_hold_s": (FG + ":DEFAULT_REFUSAL_HOLD_S", None),
    "takeoff.descent_clearance_cm": (FG + ":DESCENT_CLEARANCE_CM", None),
    "takeoff.height_cm": (TEL + ":FakeTelloController.TAKEOFF_HEIGHT_CM", OPT + "takeoff_alt_m*100"),
    "tello.ground_height_cm": (TEL + ":_GROUND_HEIGHT_CM", None),
    "tello.ground_tof_cm": (TEL + ":_GROUND_TOF_CM", None),
    "tello.ground_readings": (TEL + ":_GROUND_READINGS", None),
    "tello.temp_warn_c": (TMP + ":DEFAULT_TEMP_WARN_C", None),
    "tello.temp_refuse_c": (TMP + ":DEFAULT_TEMP_REFUSE_C", None),
    "sensing.step_cm": (OBS + ":OBSTACLE_STEP_CM", OBS + ":OBSTACLE_STEP_FIELD_CM"),
    "sensing.settle_s": (OBS + ":OBSTACLE_SETTLE_S", OBS + ":OBSTACLE_SETTLE_S"),
    "sensing.min_safe_depth": (OBS + ":OBSTACLE_MIN_SAFE_DEPTH", OBS + ":OBSTACLE_MIN_SAFE_DEPTH"),
    "sensing.person_fraction": (OBS + ":OBSTACLE_PERSON_FRACTION", OBS + ":OBSTACLE_PERSON_FRACTION"),
    "sensing.max_decode_lag_s": (OBS + ":MAX_DECODE_LAG_S", None),
    "sensing.view_wait_s": (OBS + ":OBSTACLE_FRAME_WAIT_S", OBS + ":OBSTACLE_FRAME_WAIT_S"),
    "sensing.sense_timeout_s": (OBS + ":OBSTACLE_SENSE_TIMEOUT_S", OBS + ":OBSTACLE_SENSE_TIMEOUT_S"),
    "sensing.sense_retries": (OBS + ":OBSTACLE_SENSE_RETRIES", OBS + ":OBSTACLE_SENSE_RETRIES"),
    "sensing.sensor_down_s": (OBS + ":OBSTACLE_SENSOR_DOWN_S", OBS + ":OBSTACLE_SENSOR_DOWN_S"),
    "sensing.person_wait_s": (OBS + ":OBSTACLE_DETECTION_WAIT_S", OBS + ":OBSTACLE_DETECTION_WAIT_S"),
    "move.heading_tolerance_deg": (LF + ":HEADING_TOLERANCE_DEG", MF + ":_HEADING_TOLERANCE_DEG"),
    "avoid.clear_votes": (OBS + ":OBSTACLE_CLEAR_VOTES", OBS + ":OBSTACLE_CLEAR_VOTES"),
    "avoid.relook": (OBS + ":OBSTACLE_RELOOK", OBS + ":OBSTACLE_RELOOK"),
    "avoid.sidestep_cm": (OBS + ":OBSTACLE_SIDESTEP_CM", OBS + ":OBSTACLE_SIDESTEP_CM"),
    "avoid.sidesteps": (OBS + ":OBSTACLE_SIDESTEPS", OBS + ":OBSTACLE_SIDESTEPS"),
    "avoid.sidestep_pass_cm": (OBS + ":OBSTACLE_SIDESTEP_PASS_CM", OBS + ":OBSTACLE_SIDESTEP_PASS_CM"),
    "plan.alt_slots_cm": (SAF + ":DEFAULT_ALT_SLOTS_CM", GROUND + "--slots-m*100"),
    "manual.step_cm": (MC + ":DEFAULT_STEP_CM", MC + ":DEFAULT_STEP_CM"),
    "manual.rotate_deg": (MC + ":DEFAULT_ROTATE_DEG", MC + ":DEFAULT_ROTATE_DEG"),
    "move.speed_cm_s": (None, OPT + "speed_mps*100"),
    "move.arrival_radius_cm": (None, OPT + "arrival_radius_m*100"),
    "move.arrival_alt_cm": (None, OPT + "arrival_alt_m*100"),
    "move.timeout_margin_s": (None, OPT + "move_timeout_margin_s"),
    "move.yaw_rate_dps": (None, OPT + "yaw_rate_dps"),
    "move.stationary_s": (None, OPT + "stationary_s"),
    "move.stationary_radius_cm": (None, MF + ":_STATIONARY_RADIUS_M*100"),
    "avoid.autopilot_min_margin_cm": (None, AP + ":MIN_OA_MARGIN_M*100"),
    "avoid.autopilot_min_lookahead_cm": (None, AP + ":MIN_OA_LOOKAHEAD_M*100"),
    "tello_sdk.command_port": (TB + ":TELLO_CMD_PORT", None),
    "tello_sdk.state_port": (TB + ":TELLO_STATE_PORT", None),
    "tello_sdk.video_port": (TB + ":TELLO_VIDEO_PORT", None),
    "tello_sdk.command_reply_timeout_s": (TEL + ":_COMMAND_REPLY_TIMEOUT_S", None),
    "tello_sdk.command_gap_s": (TEL + ":_TIME_BETWEEN_COMMANDS_S", None),
    "tello_sdk.state_fresh_s": (TEL + ":_STATE_FRESH_S", None),
    "tello_sdk.state_lost_s": (TEL + ":_STATE_LOST_S", None),
    "tello_sdk.motor_off_s": (TEL + ":_MOTOR_OFF_S", None),
    "tello_sdk.motor_run_gap_s": (TEL + ":_MOTOR_RUN_GAP_S", None),
    "tello_sdk.takeoff_confirm_s": (TEL + ":_TAKEOFF_CONFIRM_S", None),
    "tello_sdk.takeoff_ground_after_s": (TEL + ":_TAKEOFF_GROUND_AFTER_S", None),
    "tello_sdk.takeoff_lift_guard_s": (TEL + ":_TAKEOFF_LIFT_GUARD_S", None),
    "tello_sdk.land_deadline_s": (TEL + ":_LAND_DEADLINE_S", None),
    "tello_sdk.land_ground_wait_s": (TEL + ":_LAND_GROUND_WAIT_S", None),
    "tello_sdk.land_stall_s": (TEL + ":_LAND_STALL_S", None),
    "tello_sdk.land_descent_stall_s": (TEL + ":_LAND_DESCENT_STALL_S", None),
    "tello_sdk.land_descent_cm": (TEL + ":_LAND_DESCENT_CM", None),
    "tello_sdk.land_attempts": (TEL + ":_LAND_ATTEMPTS", None),
    "tello_sdk.connect_attempts": (TEL + ":_CONNECT_ATTEMPTS", None),
    "tello_sdk.keepalive_interval_s": (TEL + ":_KEEPALIVE_INTERVAL_S", None),
    "tello_sdk.keepalive_timeout_s": (TEL + ":_KEEPALIVE_TIMEOUT_S", None),
    "tello_sdk.tof_out_of_range_cm": (TEL + ":_TOF_OUT_OF_RANGE_CM", None),
    "tello_sdk.temp_reading_range_c": (TMP + ":TEMP_READING_RANGE_C", None),
    "retask.window_s": (LP + ":DEFAULT_HOVER_TIMEOUT_S", LP + ":DEFAULT_HOVER_TIMEOUT_S"),
    "stranded.lost_idle_s": (MC + ":DEFAULT_LOST_IDLE_S", MC + ":DEFAULT_LOST_IDLE_S"),
    "tello.refusals_per_flight": (FG + ":DEFAULT_REFUSALS_PER_FLIGHT", None),
    "lost.flying_window_s": (CO + ":LOST_OBSTACLE_S", CO + ":LOST_OBSTACLE_S"),
    "envelope.max_geofence_cm": ("ast:flightrisk/launcher/settings.py:MAX_GEOFENCE_CM", SAF + ":FIELD_LIMITS.max_geofence_cm"),
    "battery.display_warn_pct": (DS + ":SafetyPolicy.battery_warn_pct", DS + ":SafetyPolicy.battery_warn_pct"),
    "battery.display_critical_pct": (DS + ":SafetyPolicy.battery_critical_pct", DS + ":SafetyPolicy.battery_critical_pct"),
    "link.loss_action": (None, OPT + "failsafe_action~rtl=return"),
    "sensing.retry_pause_s": (LF + ":OBSTACLE_RETRY_PAUSE_S", LF + ":OBSTACLE_RETRY_PAUSE_S"),
    "plan.strip_cm": ("flightrisk.drone.plan_edit:DEFAULT_STRIP_CM", None),
    "plan.area_cm": (APP + "SINGLE_PLAN_AREA_CM", None),
    "plan.zone_gap_cm": (CO + ":DEFAULT_ZONE_GAP_CM", FS + ":field_scale(zone_gap_m)*100"),
    "separation.base_cm": (FPC + ":DEFAULT_MIN_SEPARATION_CM", FPC + ":DEFAULT_MIN_SEPARATION_CM"),
    "plan.placement_tolerance_cm": (FPC + ":DEFAULT_PLACEMENT_TOLERANCE_CM", FPC + ":DEFAULT_PLACEMENT_TOLERANCE_CM"),
    "plan.altitude_tolerance_cm": (FPC + ":ALTITUDE_TOLERANCE_CM", FPC + ":ALTITUDE_TOLERANCE_CM"),
    "plan.placement_offset_cm": (FP + ":DEFAULT_PLACEMENT_OFFSET_CM", FP + ":DEFAULT_PLACEMENT_OFFSET_CM"),
    "plan.takeoff_stagger_s": (FP + ":DEFAULT_TAKEOFF_STAGGER_S", FP + ":DEFAULT_TAKEOFF_STAGGER_S"),
    "plan.min_slot_separation_cm": (None, FS + ":MIN_SLOT_SEPARATION_M*100"),
    "plan.dedup_radius_cm": (CO + ":FleetCoordinator(dedup_radius_cm)", CO + ":FleetCoordinator(dedup_radius_cm)"),
    "plan.lost_obstacle_s": (CO + ":LOST_OBSTACLE_S", CO + ":LOST_OBSTACLE_S"),
    "plan.snap_target_cm": (PP + ":SNAP_TARGET_CM & " + PP + ":SNAP_TOLERANCE_CM", None),
    "plan.field_fence_margin_cm": (None, FS + ":_FENCE_MARGIN_M*100"),
    "airspace.close_band_floor_cm": (None, GROUND + "--slots-m[0]*100"),
    "airspace.band_gap_cm": (None, FS + ":MIN_SLOT_SEPARATION_M*100"),
    "search.required_overlap_pct": (None, FS + ":field_scale(overlap)*100"),
    "link.ground_loss_timeout_s": (APP + "FLEET_LOSS_TIMEOUT_S", APP + "FLEET_LOSS_TIMEOUT_S"),
}

# (row, profile) -> why today's value has no single runtime source. This is the one list of today values
# that are taken from parameters.md and not checked against code; the golden file repeats it under
# "doc_only_today". Any other today value without a ref stops the generator.
T, G = PROFILES
DOC_ONLY_TODAY = {
    (8, T): "instance attribute: TelloController.__init__ builds DroneCapabilities(max_altitude_m=10) (drone/tello.py:646)",
    (8, G): "instance attribute: the MAVLink controller builds DroneCapabilities(max_altitude_m=120) (drone/mavlink.py:119)",
    (17, T): "the Tello lands as soon as the link-loss watchdog fires; no constant holds the 0 s wait",
    (18, T): "behaviour: the Tello edge lands in place on link loss; no constant",
    (33, T): "FLIGHTRISK_HOVER_TEST is off unless set (drone/flight_guards.py:88); no default constant",
    (33, G): "FLIGHTRISK_HOVER_TEST is off unless set (drone/flight_guards.py:88); no default constant",
    (39, T): "FLIGHTRISK_OBSTACLE_TURN unset reads as on in its reader (edge_device.py:2040); no default constant",
    (39, G): "FLIGHTRISK_OBSTACLE_TURN unset reads as on in its reader (edge_device.py:2040); no default constant",
    (40, T): "FLIGHTRISK_OBSTACLE_SENSING unset reads as required in its reader (edge_device.py:1835); no default constant",
    (40, G): "FLIGHTRISK_OBSTACLE_SENSING unset reads as required in its reader (edge_device.py:1835); no default constant",
    (59, G): "derived marker: field lane spacing is computed from the camera swath (drone/fleet_scale.py)",
    (60, G): "per-mission marker: the field area comes with each mission",
    (70, T): "unset marker: no camera footprint is set today; the strip width is used",
    (70, G): "unset marker: no camera footprint is set today; the strip width is used",
    (71, G): "deferred marker: the field has no snap target today",
    (110, T): "the Tello frame shape (drone/tello.py:2725), unverified (drone/coverage.py:104); no constant",
    (129, T): "behaviour: a confirmed match calls _land_all (dashboard/recovery.py:185-189); no constant",
    (129, G): "behaviour: a confirmed match calls _land_all (dashboard/recovery.py:185-189); no constant",
    (143, T): "implicit: FLEET_LOSS_TIMEOUT_S minus DEFAULT_LINK_LOSS_LAND_S; tests/test_param_golden.py checks the difference",
    (143, G): "implicit: FLEET_LOSS_TIMEOUT_S minus DEFAULT_LINK_LOSS_LAND_S; tests/test_param_golden.py checks the difference",
    (158, T): "behaviour: a stranded drone lands (dashboard/manual_control.py:58-70); no constant",
    (158, G): "behaviour: a stranded drone lands (dashboard/manual_control.py:58-70); no constant",
    (159, T): "behaviour: a finished task lands when no on_complete is given; no constant",
    (159, G): "behaviour: a finished task lands when no on_complete is given; no constant",
    (160, G): "behaviour: field failsafes use autopilot RTL (drone/mavlink_fleet.py:1196-1236); no constant",
    (162, T): "derived at runtime: the sum of refused step lengths (drone/safety.py); no single value",
}

# Row id -> {profile: (lo ref, hi ref, policy)}. None for a side means that side is not bounded by a
# constant. The policy is how today's reader treats a value outside the range.
RANGES = {
    "envelope.ceiling_cm": {"tello": (SAF + ":MIN_CEILING_CM", SAF + ":MAX_CEILING_CM"),
                            "generic_multirotor": (SAF + ":FIELD_LIMITS.min_ceiling_cm", SAF + ":FIELD_LIMITS.max_ceiling_cm")},
    "envelope.ceiling_margin_cm": {"tello": (SAF + ":MIN_CEILING_MARGIN_CM", None),
                                   "generic_multirotor": (SAF + ":FIELD_LIMITS.min_ceiling_margin_cm", None)},
    "envelope.alt_slot_cm": {"tello": (SAF + ":MIN_ALT_SLOT_CM", SAF + ":MAX_ALT_SLOT_CM"),
                             "generic_multirotor": (SAF + ":FIELD_LIMITS.min_alt_slot_cm", SAF + ":FIELD_LIMITS.max_alt_slot_cm")},
    "envelope.geofence_cm": {"tello": (None, SAF + ":ROOM_LIMITS.max_geofence_cm"),
                             "generic_multirotor": (None, SAF + ":FIELD_LIMITS.max_geofence_cm")},
    "battery.min_takeoff_pct": {"tello": (SAF + ":DEFAULT_LAND_BATTERY", None),
                                "generic_multirotor": (SAF + ":DEFAULT_LAND_BATTERY", None)},
    "takeoff.settle_samples": {"tello": (FG + ":SETTLE_SAMPLES_RANGE[0]", FG + ":SETTLE_SAMPLES_RANGE[1]")},
    "takeoff.refusal_retries": {"tello": (FG + ":REFUSAL_RETRIES_RANGE[0]", FG + ":REFUSAL_RETRIES_RANGE[1]")},
    "tello.refusals_per_flight": {"tello": (FG + ":REFUSALS_PER_FLIGHT_RANGE[0]", FG + ":REFUSALS_PER_FLIGHT_RANGE[1]")},
    "tello.temp_warn_c": {"tello": (TMP + ":TEMP_LIMIT_RANGE_C[0]", TMP + ":TEMP_LIMIT_RANGE_C[1]")},
    "tello.temp_refuse_c": {"tello": (TMP + ":TEMP_LIMIT_RANGE_C[0]", TMP + ":TEMP_LIMIT_RANGE_C[1]")},
    "sensing.step_cm": {"tello": (OBS + ":OBSTACLE_STEP_MIN_CM", OBS + ":OBSTACLE_STEP_MAX_CM"),
                        "generic_multirotor": (OBS + ":OBSTACLE_STEP_MIN_CM", SAF + ":FIELD_LIMITS.max_move_cm")},
    "sensing.settle_s": {p: (OBS + ":OBSTACLE_SETTLE_RANGE_S[0]", OBS + ":OBSTACLE_SETTLE_RANGE_S[1]") for p in PROFILES},
    "sensing.min_safe_depth": {p: (OBS + ":OBSTACLE_MIN_SAFE_DEPTH_RANGE[0]", OBS + ":OBSTACLE_MIN_SAFE_DEPTH_RANGE[1]") for p in PROFILES},
    "sensing.person_fraction": {p: (OBS + ":OBSTACLE_PERSON_FRACTION_RANGE[0]", OBS + ":OBSTACLE_PERSON_FRACTION_RANGE[1]") for p in PROFILES},
    "sensing.max_decode_lag_s": {"tello": (OBS + ":MAX_DECODE_LAG_RANGE_S[0]", OBS + ":MAX_DECODE_LAG_RANGE_S[1]")},
    "avoid.clear_votes": {p: (OBS + ":OBSTACLE_CLEAR_VOTES_RANGE[0]", OBS + ":OBSTACLE_CLEAR_VOTES_RANGE[1]") for p in PROFILES},
    "avoid.relook": {p: (OBS + ":OBSTACLE_RELOOK_RANGE[0]", OBS + ":OBSTACLE_RELOOK_RANGE[1]") for p in PROFILES},
    "avoid.sidestep_cm": {p: (OBS + ":OBSTACLE_SIDESTEP_RANGE_CM[0]", OBS + ":OBSTACLE_SIDESTEP_RANGE_CM[1]") for p in PROFILES},
    "avoid.sidesteps": {p: (OBS + ":OBSTACLE_SIDESTEPS_RANGE[0]", OBS + ":OBSTACLE_SIDESTEPS_RANGE[1]") for p in PROFILES},
    "stranded.countdown_s": {p: (MC + ":MIN_ORPHAN_LAND_S", None) for p in PROFILES},
    "stranded.lost_idle_s": {p: (MC + ":MIN_LOST_IDLE_S", None) for p in PROFILES},
}
CLAMP_IDS = {"stranded.countdown_s", "stranded.lost_idle_s"}

TAG_RE = re.compile(r"from ([A-E]\d), section 27 item (\d+)")
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
MARK_RE = re.compile(r"\[(PROPOSED|UNCALIBRATED|UNMEASURED)\]")

# Cells whose text is too irregular for the generic reader. Each entry holds the default of today's
# code and the later value that a named PR switches to.
CELL_OVERRIDES = {
    (1, "tello"): {"default": 500, "pending": {"pr": "C5", "item": 29, "value": 450}},
    (12, "generic_multirotor"): {"default": 15, "pending": {"pr": "B4", "item": 8, "value": "autopilot_critical_level"}},
    (137, "generic_multirotor"): {"default": 20, "pending": {"pr": "D5", "item": 37, "value": 30}},
    # Rows 129, 144 and 145 keep today's value; the later value is pending with the PR that brings it.
    (129, "tello"): {"default": "land_all", "pending": {"pr": "E1", "item": 38, "value": "keep_watch"}},
    (129, "generic_multirotor"): {"default": "land_all", "pending": {"pr": "E1", "item": 38, "value": "keep_watch"}},
    (144, "tello"): {"default": 5.0, "pending": {"pr": "C7", "item": 34, "value": {"derived": "detect + wait + margin"}}},
    (144, "generic_multirotor"): {"default": 5.0, "pending": {"pr": "D2", "item": 15, "value": {"derived": "detect + wait + margin"}}},
    (145, "tello"): {"default": 20, "pending": {"pr": "C7", "item": 46, "value": {"derived": "20 s floor"}}},
    (145, "generic_multirotor"): {"default": 20.0, "pending": {"pr": "D2", "item": 18, "value": {
        "derived": "detect + wait + stall bound + GCS timeout + time home + landing + margin"}}},
}


def clean(cell: str) -> str:
    return cell.strip().replace("`", "")


def num(text: str):
    return float(text) if "." in text else int(text)


def parse_value(cell: str):
    """Return the literal today-style value of a default cell, or the marker for a non-numeric one."""
    text = MARK_RE.sub("", cell).strip()
    m = re.match(r"^\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)", text)
    if m:
        return [num(m.group(1)), num(m.group(2))]
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*(?:,|x)\s*(-?\d+(?:\.\d+)?)\s*$", text)
    if m:
        return [num(m.group(1)), num(m.group(2))]
    m = re.match(r"^(\d+) x (\d+)", text)
    if m:
        return [int(m.group(1)), int(m.group(2))]
    low = text.lower()
    if low.startswith("placeholder"):
        return {"placeholder": True}
    if low.startswith("derived") or low.startswith("runtime"):
        return {"derived": text}
    if low.startswith("mission area") or low.startswith("per mission"):
        return {"derived": text}
    if low.startswith("unset") or low.startswith("no default"):
        return {"unset": True}
    if low.startswith("deferred"):
        return {"deferred": True}
    m = re.match(r"^(-?\d+(?:\.\d+)?)\b", text)
    if m:
        return num(m.group(1))
    m = re.match(r"^(true|false)\b", low)
    if m:
        return m.group(1) == "true"
    m = re.match(r"^([A-Za-z_]+)", text)
    if m:
        return m.group(1)
    raise ValueError(f"cannot read the default cell {cell!r}")


def parse_pending(cell: str):
    """Return the tags of a cell as pending entries; the later value is the text before the tag."""
    out = []
    for m in TAG_RE.finditer(cell):
        before = cell[:m.start()]
        m2 = re.search(r"\(([^()]*?)\s*$", before)
        value = None
        if m2:
            token = m2.group(1).strip()
            if NUM_RE.match(token):
                value = num(token)
            elif re.match(r"^[a-z_]+$", token):
                value = token
        out.append({"pr": m.group(1), "item": int(m.group(2)), "value": value})
    return out


def parse_bounds(cell: str):
    text = MARK_RE.sub("", cell).strip()
    out = {"lo": None, "hi": None, "off_value": None, "text": text}
    m = re.match(r"^(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\b", text)
    if m:
        out["lo"], out["hi"] = num(m.group(1)), num(m.group(2))
    m = re.search(r"off (\d+)", text)
    if m:
        out["off_value"] = int(m.group(1))
    elif not text.startswith("{") and re.search(r",\s*off\b", text):
        out["off_value"] = "off"
    if text.startswith("fixed"):
        out["fixed"] = True
    m = re.match(r"^\{(.*)\}", text)
    if m:
        out["choices"] = [c.strip() for c in m.group(1).split(",")]
    return out


def parse_edit(cell: str):
    text = cell.strip()
    m = re.match(r"^(\w+) \(Tello\), (\w+) \(generic\)$", text)
    if m:
        return {"tello": m.group(1), "generic_multirotor": m.group(2)}
    word = re.match(r"^(\w+)", text).group(1)
    return {"tello": word, "generic_multirotor": word}


def read_rows(lines):
    rows = []
    for line in lines:
        if not re.match(r"^\| \d+ \|", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[1] == "removed":
            continue
        if len(cells) == 16:
            rows.append(cells)
        elif "removed" not in line:
            raise ValueError(f"unexpected row shape: {line[:80]}")
    return rows


def _ast_value(path: str, name: str):
    """A module-level literal, or the default of an argparse flag, read from source without importing it."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    if name.startswith("--"):
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument"
                    and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == name):
                for kw in node.keywords:
                    if kw.arg == "default":
                        return ast.literal_eval(kw.value)
    else:
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return ast.literal_eval(node.value)
    raise LookupError(f"{name} not found in {path}")


def _scaled(value, scale):
    if scale == 1 or isinstance(value, bool):
        return value
    out = value * scale
    return round(out, 6) if isinstance(out, float) else out


def resolve(ref: str):
    """Read the runtime value a ref names (see TODAY_REFS for the syntax)."""
    if " & " in ref:
        return [resolve(part) for part in ref.split(" & ")]
    rename = {}
    if "~" in ref:
        ref, pairs = ref.split("~", 1)
        rename = dict(pair.split("=", 1) for pair in pairs.split(","))
    scale = 1
    if "*" in ref:
        ref, s = ref.rsplit("*", 1)
        scale = float(s) if "." in s else int(s)
    index = None
    m = re.match(r"^(.*)\[(\d+)\]$", ref)
    if m:
        ref, index = m.group(1), int(m.group(2))
    if ref.startswith("ast:"):
        _, path, name = ref.split(":", 2)
        obj = _ast_value(path, name)
    else:
        module, path = ref.split(":")
        param = None
        m = re.match(r"^(.*)\((\w+)\)$", path)
        if m:
            path, param = m.group(1), m.group(2)
        obj = importlib.import_module(module)
        for part in path.split("."):
            obj = getattr(obj, part)
        if param:
            obj = inspect.signature(obj).parameters[param].default
    if index is not None:
        obj = obj[index]
    if isinstance(obj, (tuple, list)):
        return [_scaled(x, scale) for x in obj]
    if isinstance(obj, float) and obj == float("inf"):
        return None
    obj = _scaled(obj, scale)
    return rename.get(obj, obj)


def alias_scale(name: str, refs) -> tuple:
    """The factor that turns a value given under ``name`` into the registry unit, and where it comes from.

    A CLI flag whose argparse destination is the attribute a today_ref reads (``--speed-mps`` feeds
    ``MavlinkFleetOptions.speed_mps``) converts by that ref's factor, so the scale is taken from the code.
    Otherwise a metre or metre-per-second name is scaled by 100, and every other name by 1.
    """
    suffix = 100 if name.lower().endswith(("_m", "-m", "_mps", "-mps")) else 1
    if name.startswith("--"):
        dest = name[2:].replace("-", "_")
        for ref in refs:
            if not ref or "*" not in ref:
                continue
            base, factor = ref.rsplit("*", 1)
            if re.split(r"[.:]", base)[-1] == dest:
                scale = float(factor) if "." in factor else int(factor)
                if scale != suffix:
                    raise ValueError(f"{name}: scale {scale} from {ref} disagrees with its name")
                return scale, ref
    return suffix, "name"


def range_entry(refs):
    lo_ref, hi_ref = refs
    return {
        "lo": resolve(lo_ref) if lo_ref else None,
        "hi": resolve(hi_ref) if hi_ref else None,
        "lo_ref": lo_ref,
        "hi_ref": hi_ref,
    }


def build():
    text = PARAMS_MD.read_text(encoding="utf-8")
    sec1 = text.split("## 1. Registry rows", 1)[1].split("### 1.1", 1)[0]
    sec11 = text.split("### 1.1", 1)[1].split("\n## 2.", 1)[0]
    rows_out = []
    doc_only = []
    for cells in read_rows(sec1.splitlines()):
        n = int(cells[0])
        ids = re.findall(r"`([a-z_0-9.]+)`", cells[1])
        pid = ids[0]
        unit = cells[4]
        notes = cells[15]
        aliases = []
        refs = TODAY_REFS.get(pid, (None, None))
        # Environment names and flags are named in the Notes column, and for a few rows in the Today column.
        for name in re.findall(r"`((?:FLIGHTRISK_[A-Z0-9_]+)|(?:--[a-z0-9-]+))`", notes + " " + cells[3]):
            if name not in [a["name"] for a in aliases]:
                scale, scale_from = alias_scale(name, refs)
                aliases.append({"name": name, "scale": scale, "scale_from": scale_from})
        today_text = clean(cells[3])
        default_cells = {"tello": clean(cells[7]), "generic_multirotor": clean(cells[8])}
        bound_cells = {"tello": clean(cells[9]), "generic_multirotor": clean(cells[10])}
        edits = parse_edit(cells[13])
        profiles = {}
        for i, prof in enumerate(PROFILES):
            dcell = default_cells[prof]
            if dcell.lower().startswith("n/a"):
                profiles[prof] = None
                continue
            over = CELL_OVERRIDES.get((n, prof))
            pending = parse_pending(dcell) + [dict(p, where="bounds") for p in parse_pending(bound_cells[prof])]
            if over:
                default = over["default"]
                pending = [over["pending"]] + [p for p in pending if p.get("where") == "bounds"]
            else:
                default = parse_value(dcell)
            b = parse_bounds(bound_cells[prof])
            lo, hi = b["lo"], b["hi"]
            if b.get("fixed") and isinstance(default, (int, float)) and not isinstance(default, bool):
                lo = hi = default
            ref = refs[i]
            entry = {
                "default": default,
                "lo": lo,
                "hi": hi,
                "bound_text": b["text"],
                "safer": clean(cells[11]),
                "confirm": clean(cells[12]),
                "edit": edits[prof],
                "off_value": b["off_value"],
                "safety_switch": pid in SAFETY_SWITCH_IDS,
                "marks": MARK_RE.findall(dcell),
                "pending": pending,
                "origin": "new",
                "bounds_origin": "new" if "new" in bound_cells[prof].replace("from", "") else "today",
                "today_ref": None,
            }
            if "choices" in b:
                entry["choices"] = b["choices"]
            if ref:
                value = resolve(ref)
                if isinstance(value, tuple):
                    value = list(value)
                if value != default:
                    raise ValueError(
                        f"row {n} {pid} {prof}: table default {default!r} differs from {ref} = {value!r}")
                entry["origin"] = "today"
                entry["today_ref"] = ref
            elif (n, prof) in DOC_ONLY_TODAY:
                entry["origin"] = "today"
                entry["today_ref_reason"] = DOC_ONLY_TODAY[(n, prof)]
                doc_only.append({"row": n, "id": pid, "profile": prof, "reason": DOC_ONLY_TODAY[(n, prof)]})
            elif n < 111 and today_text.lower() not in ("none", ""):
                # Rows 111 to 162 are new surfaces whose Today column describes related behaviour; they are
                # today values only with a ref or a DOC_ONLY_TODAY entry.
                raise ValueError(f"row {n} {pid} {prof}: a today value needs a TODAY_REFS ref or a DOC_ONLY_TODAY reason")
            profiles[prof] = entry
        accepts = {}
        for prof, spec in RANGES.get(pid, {}).items():
            if profiles.get(prof) is not None:
                accepts[prof] = dict(range_entry(spec), policy="clamp" if pid in CLAMP_IDS else "refuse")
        alias_out = {}
        for a in aliases:
            alias_out[a["name"]] = {
                "scale": a["scale"],
                "scale_from": a["scale_from"],
                "today_accepts": accepts or None,
                "today_accepts_reason": None if accepts else "no importable range constant for this row",
            }
        rows_out.append({
            "row": n,
            "id": pid,
            "ids": ids,
            "unit": unit,
            "today_text": today_text,
            "applies": clean(cells[14]),
            "env_aliases": alias_out,
            "profiles": profiles,
        })
    constraints = []
    for line in sec11.splitlines():
        m = re.match(r"^\| (K-\d+) \| (.*) \| ([^|]*) \|$", line)
        if m:
            constraints.append({"id": m.group(1), "text": clean(m.group(2)), "where": clean(m.group(3))})
    unused = set(DOC_ONLY_TODAY) - {(d["row"], d["profile"]) for d in doc_only}
    if unused:
        raise ValueError(f"DOC_ONLY_TODAY entries not used: {sorted(unused)}")
    return {"schema": 1, "source": "docs/design/hardware-profiles/parameters.md",
            "rows": rows_out, "constraints": constraints, "doc_only_today": doc_only}


def main(argv):
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    if out.exists():
        print(f"refusing to overwrite {out}: the golden file is generated once; delete it by hand to regenerate",
              file=sys.stderr)
        return 2
    data = build()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} with {len(data['rows'])} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
