#!/usr/bin/env python3
"""MAVLink vectors for the phone mission core: golden codec frames and client transcripts.

This module writes two files in ``tests/fixtures/mission_core/``:

* ``mavlink_frames.json``: MAVLink 2 frames that pymavlink encodes, for the message subset of the phone
  clients (plan section 2.6). The Swift codec (MAVLinkLite) must encode every frame byte for byte and decode
  it back to the listed fields; the Kotlin client (mavlink-kotlin) must decode every frame. The file also
  holds the wire layout of each subset message, frames that a decoder must skip, and one byte stream that
  mixes good and bad frames.
* ``mavlink_transcripts.json``: scripted exchanges between a phone client and an ArduPilot or PX4 vehicle,
  one or more per behaviour of plan section 2.6 (and the link rules of sections 1.1 and 2.4). Each transcript
  names the autopilot behaviour it encodes and cites the autopilot source or the MAVLink documentation that
  shows it. The exchanges are hand-authored spec by example, written here as Python data so that every
  message is checked against pymavlink's message definitions while the file is generated.

Interface for ``scripts/make_mission_core_vectors.py``: ``OUTPUTS`` maps each output file name to a function
that returns its JSON-ready object, and ``render(obj)`` gives the exact file text. Run this module directly
with ``--check`` to compare the committed files with a fresh generation, or with ``--write`` to rewrite them.

pymavlink is a development dependency only (``requirements-dev.txt``). Its generated code is MIT licensed and
the tool is LGPL-3.0; nothing of it ships in either app.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import io
import json
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "tests" / "fixtures" / "mission_core"
GENERATOR = "scripts/mission_vectors/mavlink_frames.py"
PYMAVLINK_VERSION = "2.4.50"

GCS_SYSID = 255
GCS_COMPID = 190  # MAV_COMP_ID_MISSIONPLANNER, the id QGroundControl uses.

# Plan section 2.6 lists 18 messages. The source reading for this task added two that a vehicle sends to the
# phone: ArduCopter requests mission items with MISSION_REQUEST, never MISSION_REQUEST_INT, and ArduCopter
# 4.7 answers a read of an unknown parameter name with PARAM_ERROR.
SUBSET = (
    "HEARTBEAT",
    "SYS_STATUS",
    "BATTERY_STATUS",
    "GPS_RAW_INT",
    "GLOBAL_POSITION_INT",
    "HOME_POSITION",
    "EXTENDED_SYS_STATE",
    "STATUSTEXT",
    "COMMAND_LONG",
    "COMMAND_ACK",
    "PARAM_REQUEST_READ",
    "PARAM_VALUE",
    "PARAM_ERROR",
    "MISSION_COUNT",
    "MISSION_REQUEST",
    "MISSION_REQUEST_INT",
    "MISSION_ITEM_INT",
    "MISSION_ACK",
    "MISSION_CURRENT",
    "AUTOPILOT_VERSION",
)
ADDED_TO_PLAN_SUBSET = ("MISSION_REQUEST", "PARAM_ERROR")


# --------------------------------------------------------------------------------------------------------
# pymavlink access
# --------------------------------------------------------------------------------------------------------


def _dialect():
    from pymavlink.dialects.v20 import common

    return common


def _installed_pymavlink() -> str:
    from importlib.metadata import version

    return version("pymavlink")


@functools.lru_cache(maxsize=1)
def _xml_messages() -> dict:
    """Message definitions from the XML that pymavlink ships (common and its includes), keyed by name."""
    from pymavlink.generator import mavparse

    import pymavlink

    out = {}
    for fname in ("minimal.xml", "standard.xml", "common.xml"):
        path = Path(pymavlink.__file__).parent / "dialects" / "v20" / fname
        with contextlib.redirect_stdout(io.StringIO()):
            xml = mavparse.MAVXML(str(path), mavparse.PROTOCOL_2_0)
        out.update({m.name: m for m in xml.message})
    return out


def _msg_class(name: str):
    return getattr(_dialect(), "MAVLink_%s_message" % name.lower())


def _f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _f32_bits(value: float) -> str:
    return struct.pack(">f", value).hex()


def _field_types(name: str) -> dict:
    cls = _msg_class(name)
    # pymavlink lists fieldtypes in declaration order and array_lengths in wire order.
    lengths = dict(zip(cls.ordered_fieldnames, cls.array_lengths))
    return {f: (t, lengths[f]) for f, t in zip(cls.fieldnames, cls.fieldtypes)}


def _to_pymavlink(name: str, fields: dict) -> dict:
    """JSON field values to the arguments a pymavlink encoder takes. Missing fields are zero."""
    out = {}
    for field, (ftype, alen) in _field_types(name).items():
        value = fields.get(field)
        if ftype == "char":
            out[field] = (value or "").encode("ascii")
        elif alen:
            items = list(value) if value is not None else []
            if len(items) > alen:
                raise ValueError("%s.%s has more than %d items" % (name, field, alen))
            items += [0] * (alen - len(items))
            out[field] = [float("nan") if v == "NaN" else v for v in items]
        elif ftype in ("float", "double"):
            out[field] = float("nan") if value == "NaN" else float(value or 0)
        else:
            out[field] = int(value or 0)
    unknown = set(fields) - set(out)
    if unknown:
        raise ValueError("%s has no field %s" % (name, sorted(unknown)))
    return out


def _json_value(ftype: str, value):
    if isinstance(value, (list, tuple)):
        return [_json_value(ftype, v) for v in value]
    if ftype in ("float", "double"):
        return "NaN" if math.isnan(value) else _f32(value) if ftype == "float" else float(value)
    if isinstance(value, bytes):
        return value.split(b"\x00", 1)[0].decode("ascii")
    return value


def encode(name: str, fields: dict, *, sysid: int, compid: int, seq: int) -> bytes:
    """One MAVLink 2 frame, encoded by pymavlink. Raises if a field is unknown or out of range."""
    dialect = _dialect()
    mav = dialect.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    mav.seq = seq
    encoder = getattr(mav, "%s_encode" % name.lower())
    msg = encoder(**_to_pymavlink(name, fields))
    return bytes(msg.pack(mav))


def decode_fields(frame: bytes) -> tuple[str, dict]:
    """Decode one frame with pymavlink to (message name, every field as JSON values)."""
    dialect = _dialect()
    mav = dialect.MAVLink(None)
    msg = mav.decode(bytearray(frame))
    name = msg.get_type()
    types = _field_types(name)
    fields = {f: _json_value(types[f][0], getattr(msg, f)) for f in msg.fieldnames}
    return name, fields


# --------------------------------------------------------------------------------------------------------
# mavlink_frames.json
# --------------------------------------------------------------------------------------------------------


def _layouts() -> dict:
    xml = _xml_messages()
    out = {}
    for name in SUBSET:
        m = xml[name]
        ext = set(f.name for f in m.fields[m.extensions_start:]) if m.extensions_start is not None else set()
        out[name] = {
            "id": m.id,
            "crc_extra": m.crc_extra,
            "min_len": m.wire_min_length,
            "max_len": m.wire_length,
            "fields": [
                {
                    "name": f.name,
                    "type": f.type,
                    "array": f.array_length or None,
                    "offset": f.wire_offset,
                    "extension": f.name in ext,
                }
                for f in m.ordered_fields
            ],
        }
    return out


def _px4_mode(main: int, sub: int = 0) -> int:
    return (main << 16) | (sub << 24)


def _int32_as_float(value: int) -> float:
    return struct.unpack("<f", struct.pack("<i", value))[0]


# (frame name, message, sysid, compid, seq, fields, note). Vehicle frames use sysid 1 and compid 1.
_FRAMES = [
    ("heartbeat_gcs", "HEARTBEAT", GCS_SYSID, GCS_COMPID, 0,
     dict(type=6, autopilot=8, base_mode=0, custom_mode=0, system_status=4, mavlink_version=3),
     "The phone's own heartbeat: MAV_TYPE_GCS, MAV_AUTOPILOT_INVALID."),
    ("heartbeat_ardupilot_guided_armed", "HEARTBEAT", 1, 1, 17,
     dict(type=2, autopilot=3, base_mode=217, custom_mode=4, system_status=4, mavlink_version=3),
     "ArduCopter in GUIDED, armed, flying."),
    ("heartbeat_px4_mission_armed", "HEARTBEAT", 1, 1, 200,
     dict(type=2, autopilot=12, base_mode=157, custom_mode=_px4_mode(4, 4), system_status=4, mavlink_version=3),
     "PX4 in AUTO.MISSION, armed: custom_mode packs main mode 4 in byte 2 and sub mode 4 in byte 3."),
    ("sys_status", "SYS_STATUS", 1, 1, 18,
     dict(onboard_control_sensors_present=0x1260FC2F, onboard_control_sensors_enabled=0x1260FC2F,
          onboard_control_sensors_health=0x1260FC2F, load=312, voltage_battery=15800, current_battery=1530,
          drop_rate_comm=0, errors_comm=0, battery_remaining=76),
     "Extension fields are zero, so the payload is truncated to 31 bytes."),
    ("sys_status_battery_unknown", "SYS_STATUS", 1, 1, 19,
     dict(onboard_control_sensors_present=0x1260FC2F, voltage_battery=65535, current_battery=-1,
          battery_remaining=-1, onboard_control_sensors_present_extended=1),
     "battery_remaining -1 and voltage_battery UINT16_MAX mean unknown; a nonzero extension keeps 43 bytes."),
    ("battery_status", "BATTERY_STATUS", 1, 1, 20,
     dict(id=0, battery_function=1, type=1, temperature=3150,
          voltages=[4200, 4190, 4180, 4170, 65535, 65535, 65535, 65535, 65535, 65535],
          current_battery=1530, current_consumed=812, energy_consumed=-1, battery_remaining=74,
          time_remaining=0, charge_state=1),
     "Battery 0, four cells reported; unused cells are UINT16_MAX."),
    ("battery_status_second_pack", "BATTERY_STATUS", 1, 1, 21,
     dict(id=1, battery_function=1, type=1, temperature=32767, voltages=[65535] * 10,
          current_battery=-1, current_consumed=-1, energy_consumed=-1, battery_remaining=-1),
     "A second battery instance; the phone reads instance 0 only."),
    ("gps_raw_int", "GPS_RAW_INT", 1, 1, 22,
     dict(time_usec=123456789012, fix_type=3, lat=406894000, lon=-1117310000, alt=1600120, eph=121, epv=180,
          vel=310, cog=9000, satellites_visible=14, alt_ellipsoid=1585300, h_acc=900, v_acc=1400,
          vel_acc=200, hdg_acc=0, yaw=0),
     "3D fix, 14 satellites; negative longitude."),
    ("gps_raw_int_no_fix", "GPS_RAW_INT", 1, 1, 23,
     dict(time_usec=5000000, fix_type=1, lat=0, lon=0, alt=0, eph=65535, epv=65535, vel=65535, cog=65535,
          satellites_visible=255),
     "No fix: satellites_visible 255 means unknown."),
    ("global_position_int", "GLOBAL_POSITION_INT", 1, 1, 24,
     dict(time_boot_ms=184200, lat=406894450, lon=-1117306000, alt=1610050, relative_alt=10050, vx=300,
          vy=-12, vz=-5, hdg=9000),
     "relative_alt is millimetres above home; hdg is centidegrees."),
    ("global_position_int_below_home_heading_unknown", "GLOBAL_POSITION_INT", 1, 1, 25,
     dict(time_boot_ms=4200, lat=406894000, lon=-1117310000, alt=1599750, relative_alt=-250, hdg=65535),
     "Negative relative_alt; hdg UINT16_MAX means unknown."),
    ("home_position", "HOME_POSITION", 1, 1, 26,
     dict(latitude=406894000, longitude=-1117310000, altitude=1600000, x=0.0, y=0.0, z=0.0,
          q=[1.0, 0.0, 0.0, 0.0], approach_x=0.0, approach_y=0.0, approach_z=0.0, time_usec=0),
     "Home with an identity quaternion; time_usec (an extension) is zero, so the frame is truncated."),
    ("extended_sys_state_in_air", "EXTENDED_SYS_STATE", 1, 1, 27,
     dict(vtol_state=3, landed_state=2),
     "ArduCopter reports MAV_VTOL_STATE_MC (3); landed_state 2 is IN_AIR."),
    ("extended_sys_state_on_ground", "EXTENDED_SYS_STATE", 1, 1, 28,
     dict(vtol_state=0, landed_state=1),
     "PX4 multicopter: vtol_state 0, landed_state 1 is ON_GROUND."),
    ("statustext_prearm", "STATUSTEXT", 1, 1, 29,
     dict(severity=2, text="PreArm: Need Position Estimate"),
     "Trailing zero bytes of the text are truncated from the payload; a decoder zero-fills them."),
    ("statustext_full_50", "STATUSTEXT", 1, 1, 30,
     dict(severity=6, text="ArduCopter V4.6.3 (92b0cd78) quad frame, 4 motors x"),
     "Exactly 50 characters: the text has no terminating NUL."),
    ("command_long_arm", "COMMAND_LONG", GCS_SYSID, GCS_COMPID, 1,
     dict(target_system=1, target_component=1, command=400, confirmation=0, param1=1.0),
     "MAV_CMD_COMPONENT_ARM_DISARM; confirmation 0 is the last byte, so it is truncated."),
    ("command_long_set_message_interval", "COMMAND_LONG", GCS_SYSID, GCS_COMPID, 2,
     dict(target_system=1, target_component=1, command=511, confirmation=0, param1=33.0, param2=200000.0),
     "MAV_CMD_SET_MESSAGE_INTERVAL for GLOBAL_POSITION_INT at 5 Hz."),
    ("command_long_px4_hold_retry", "COMMAND_LONG", GCS_SYSID, GCS_COMPID, 3,
     dict(target_system=1, target_component=1, command=176, confirmation=1, param1=1.0, param2=4.0,
          param3=3.0),
     "MAV_CMD_DO_SET_MODE to PX4 AUTO.LOITER, second try (confirmation 1)."),
    ("command_long_land_third_try", "COMMAND_LONG", GCS_SYSID, GCS_COMPID, 255,
     dict(target_system=1, target_component=1, command=21, confirmation=2),
     "MAV_CMD_NAV_LAND, third try; sequence number 255 (the next frame wraps to 0)."),
    ("command_long_request_home", "COMMAND_LONG", GCS_SYSID, GCS_COMPID, 0,
     dict(target_system=1, target_component=1, command=512, confirmation=0, param1=242.0),
     "MAV_CMD_REQUEST_MESSAGE for HOME_POSITION; sequence number 0 after the wrap."),
    ("command_ack_accepted_ardupilot", "COMMAND_ACK", 1, 1, 31,
     dict(command=400, result=0, progress=0, result_param2=0, target_system=255, target_component=190),
     "ArduCopter addresses the ACK to the sender (extension fields target_system and target_component)."),
    ("command_ack_denied_px4", "COMMAND_ACK", 1, 1, 32,
     dict(command=400, result=2, target_system=255, target_component=190),
     "PX4 arm refused by its preflight checks: MAV_RESULT_DENIED."),
    ("command_ack_no_target", "COMMAND_ACK", 1, 1, 33,
     dict(command=176, result=0),
     "All extension fields zero: the payload is 3 bytes."),
    ("param_request_read_by_name", "PARAM_REQUEST_READ", GCS_SYSID, GCS_COMPID, 4,
     dict(target_system=1, target_component=1, param_id="FS_GCS_ENABLE", param_index=-1),
     "param_index -1 selects the name; trailing NULs of param_id are truncated."),
    ("param_request_read_16_chars", "PARAM_REQUEST_READ", GCS_SYSID, GCS_COMPID, 5,
     dict(target_system=1, target_component=1, param_id="FENCE_ALT_MAX_TP", param_index=-1),
     "A 16-character name has no terminating NUL."),
    ("param_value_ardupilot_int8", "PARAM_VALUE", 1, 1, 34,
     dict(param_id="FS_GCS_ENABLE", param_value=1.0, param_type=2, param_count=1250, param_index=65535),
     "ArduCopter: INT8 sent as a C cast to float; by-name reads echo index -1 (65535)."),
    ("param_value_ardupilot_real32", "PARAM_VALUE", 1, 1, 35,
     dict(param_id="FS_GCS_TIMEOUT", param_value=5.0, param_type=9, param_count=1250, param_index=65535),
     "ArduCopter REAL32."),
    ("param_value_px4_int32_bytewise", "PARAM_VALUE", 1, 1, 36,
     dict(param_id="NAV_DLL_ACT", param_value=_int32_as_float(2), param_type=6, param_count=1027,
          param_index=611),
     "PX4: the int32 2 is copied byte-wise into the float field (a subnormal float)."),
    ("param_value_px4_int32_negative_bytewise", "PARAM_VALUE", 1, 1, 37,
     dict(param_id="FR_SYNTH_NEG", param_value=_int32_as_float(-1), param_type=6, param_count=1027,
          param_index=12),
     "Synthetic name. The int32 -1 byte-wise is the float bit pattern ffffffff, a NaN: a decoder must keep "
     "the bits and never canonicalise the NaN."),
    ("param_value_px4_real32", "PARAM_VALUE", 1, 1, 38,
     dict(param_id="GF_MAX_HOR_DIST", param_value=120.0, param_type=9, param_count=1027, param_index=402),
     "PX4 REAL32."),
    ("param_error_does_not_exist", "PARAM_ERROR", 1, 1, 39,
     dict(target_system=255, target_component=190, param_id="SYSID_MYGCS", param_index=-1, error=1),
     "ArduCopter 4.7.3 answers a read of an unknown name with MAV_PARAM_ERROR_DOES_NOT_EXIST."),
    ("mission_count", "MISSION_COUNT", GCS_SYSID, GCS_COMPID, 6,
     dict(target_system=1, target_component=1, count=7, mission_type=0),
     "Upload start; mission_type 0 is MAV_MISSION_TYPE_MISSION."),
    ("mission_request_ardupilot", "MISSION_REQUEST", 1, 1, 40,
     dict(target_system=255, target_component=190, seq=0, mission_type=0),
     "ArduCopter requests items with MISSION_REQUEST (40)."),
    ("mission_request_int_px4", "MISSION_REQUEST_INT", 1, 1, 41,
     dict(target_system=255, target_component=190, seq=3, mission_type=0),
     "PX4 requests items with MISSION_REQUEST_INT (51)."),
    ("mission_item_int_home_placeholder", "MISSION_ITEM_INT", GCS_SYSID, GCS_COMPID, 7,
     dict(target_system=1, target_component=1, seq=0, frame=0, command=16, current=0, autocontinue=1,
          x=406894000, y=-1117310000, z=0.0),
     "ArduPilot sequence 0: the home placeholder."),
    ("mission_item_int_takeoff", "MISSION_ITEM_INT", GCS_SYSID, GCS_COMPID, 8,
     dict(target_system=1, target_component=1, seq=1, frame=3, command=22, current=0, autocontinue=1,
          param4="NaN", x=406894000, y=-1117310000, z=10.0),
     "NAV_TAKEOFF to 10 m relative to home; yaw NaN keeps the autopilot's heading behaviour."),
    ("mission_item_int_waypoint", "MISSION_ITEM_INT", GCS_SYSID, GCS_COMPID, 9,
     dict(target_system=1, target_component=1, seq=3, frame=3, command=16, current=0, autocontinue=1,
          param4="NaN", x=406894450, y=-1117310000, z=10.0),
     "NAV_WAYPOINT in MAV_FRAME_GLOBAL_RELATIVE_ALT."),
    ("mission_item_int_change_speed", "MISSION_ITEM_INT", GCS_SYSID, GCS_COMPID, 10,
     dict(target_system=1, target_component=1, seq=2, frame=2, command=178, current=0, autocontinue=1,
          param1=1.0, param2=3.0, param3=-1.0),
     "DO_CHANGE_SPEED in MAV_FRAME_MISSION: ground speed 3 m/s, throttle unchanged."),
    ("mission_item_int_return", "MISSION_ITEM_INT", GCS_SYSID, GCS_COMPID, 11,
     dict(target_system=1, target_component=1, seq=6, frame=2, command=20, current=0, autocontinue=1),
     "NAV_RETURN_TO_LAUNCH in MAV_FRAME_MISSION; mission_type 0 is truncated."),
    ("mission_ack_accepted_px4", "MISSION_ACK", 1, 1, 42,
     dict(target_system=255, target_component=190, type=0, mission_type=0),
     "Upload accepted. PX4 1.17 appends opaque_id (a newer extension); see 'extended_frames'."),
    ("mission_ack_no_space_ardupilot", "MISSION_ACK", 1, 1, 43,
     dict(target_system=255, target_component=190, type=4, mission_type=0),
     "MAV_MISSION_NO_SPACE."),
    ("mission_current_full", "MISSION_CURRENT", 1, 1, 44,
     dict(seq=3, total=7, mission_state=3, mission_mode=1),
     "Current item 3 of 7, mission active."),
    ("mission_current_seq_only", "MISSION_CURRENT", 1, 1, 45,
     dict(seq=1),
     "Only seq set: the payload is truncated to 1 byte, and a decoder zero-fills the rest."),
    ("autopilot_version_ardupilot", "AUTOPILOT_VERSION", 1, 1, 46,
     dict(capabilities=0xFDEF, flight_sw_version=0x040603FF, middleware_sw_version=0, os_sw_version=0,
          board_version=0, flight_custom_version=[0x39, 0x32, 0x62, 0x30, 0x63, 0x64, 0x37, 0x38],
          vendor_id=0, product_id=0, uid=0, uid2=[0] * 18),
     "ArduCopter 4.6.3, official (type byte 255)."),
    ("autopilot_version_px4", "AUTOPILOT_VERSION", 1, 1, 47,
     dict(capabilities=0x3E3F, flight_sw_version=0x011100FF, middleware_sw_version=0x011100FF,
          os_sw_version=0x0B000000, board_version=0, flight_custom_version=[0xD6, 0xF1, 0x2A, 0xD1, 0, 0, 0, 0],
          vendor_id=0, product_id=0, uid=0x0001020304050607, uid2=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
                                                                     13, 14, 15, 16, 17, 18]),
     "PX4 1.17.0; uid2 set, so the frame is not truncated."),
]


def _frame_record(name, msg, sysid, compid, seq, fields, note) -> dict:
    data = encode(msg, fields, sysid=sysid, compid=compid, seq=seq)
    decoded_name, decoded = decode_fields(data)
    assert decoded_name == msg
    layout_types = _field_types(msg)
    bits = {}
    for field, (ftype, alen) in layout_types.items():
        if ftype != "float":
            continue
        pv = _to_pymavlink(msg, fields)[field]
        bits[field] = [_f32_bits(v) for v in pv] if alen else _f32_bits(pv)
    xml = _xml_messages()[msg]
    record = {
        "name": name,
        "msg": msg,
        "msgid": xml.id,
        "sysid": sysid,
        "compid": compid,
        "seq": seq,
        "payload_len": data[1],
        "truncated": data[1] < xml.wire_length,
        "fields": decoded,
        "float_bits": bits,
        "hex": data.hex(),
        "note": note,
    }
    if msg == "PARAM_VALUE":
        raw = struct.unpack("<i", struct.pack("<f", fields["param_value"]))[0]
        record["value"] = (
            {"encoding": "bytewise", "int": raw} if fields["param_type"] in (1, 2, 3, 4, 5, 6) and "px4" in name
            else {"encoding": "c_cast", "number": fields["param_value"]}
        )
    return record


def _crc_x25(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        tmp = byte ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def _raw_v2(msgid: int, payload: bytes, *, seq: int, sysid: int, compid: int, crc_extra: int,
            incompat: int = 0, signature: bytes = b"") -> bytes:
    header = bytes([len(payload), incompat, 0, seq, sysid, compid]) + msgid.to_bytes(3, "little")
    crc = _crc_x25(header + payload + bytes([crc_extra]))
    return b"\xfd" + header + payload + crc.to_bytes(2, "little") + signature


def _bad_frames(frames: dict) -> list:
    good = bytes.fromhex(frames["heartbeat_ardupilot_guided_armed"]["hex"])
    bad_crc = good[:-1] + bytes([good[-1] ^ 0xFF])
    truncated = bytes.fromhex(frames["global_position_int"]["hex"])[:20]
    unknown = _raw_v2(50001, bytes(range(1, 9)), seq=50, sysid=1, compid=1, crc_extra=0)
    signed = _raw_v2(0, good[10:19], seq=51, sysid=1, compid=1, crc_extra=50, incompat=0x01,
                     signature=bytes([1]) + bytes(range(0x10, 0x1C)))
    unknown_flag = _raw_v2(0, good[10:19], seq=52, sysid=1, compid=1, crc_extra=50, incompat=0x02)
    v1 = bytes([0xFE, 9, 53, 1, 1, 0]) + good[10:19]
    v1 += _crc_x25(v1[1:] + bytes([50])).to_bytes(2, "little")
    return [
        {"name": "bad_crc", "hex": bad_crc.hex(), "skip": "the checksum does not match"},
        {"name": "truncated", "hex": truncated.hex(), "skip": "the buffer ends inside the frame"},
        {"name": "unknown_message", "hex": unknown.hex(),
         "skip": "message id 50001 is not in the subset, so its checksum cannot be checked: produce nothing and resume the search at the next byte"},
        {"name": "signed", "hex": signed.hex(),
         "skip": "incompat flag 0x01 (signed): produce nothing; v1 links are unsigned"},
        {"name": "unknown_incompat_flag", "hex": unknown_flag.hex(),
         "skip": "an incompat flag the codec does not know: drop the frame (MAVLink 2 rule)"},
        {"name": "mavlink1", "hex": v1.hex(), "skip": "MAVLink 1 frame (0xFE); v1 speaks MAVLink 2 only"},
    ]


def _extended_frames(frames: dict) -> list:
    """Frames whose payload is longer than pymavlink 2.4.50's definition: newer firmware appends extension
    fields (PX4 1.17 sends MISSION_ACK.opaque_id and MISSION_CURRENT.mission_id). The CRC_EXTRA excludes
    extensions, so the checksum still validates; a decoder reads the known fields and ignores the rest."""
    out = []
    for name, extra, note in (
        ("mission_ack_accepted_px4", (0x5A3C9E01).to_bytes(4, "little"),
         "MISSION_ACK with PX4's opaque_id (mission checksum) appended."),
        ("mission_current_full", (0x5A3C9E01).to_bytes(4, "little") + bytes(8),
         "MISSION_CURRENT with mission_id, fence_id and rally_points_id appended."),
    ):
        base = frames[name]
        raw = bytes.fromhex(base["hex"])
        xml = _xml_messages()[base["msg"]]
        payload = raw[10:10 + raw[1]]
        payload += bytes(xml.wire_length - len(payload)) + extra
        frame = _raw_v2(xml.id, payload, seq=base["seq"], sysid=base["sysid"], compid=base["compid"],
                        crc_extra=xml.crc_extra)
        out.append({"name": name + "_extended", "msg": base["msg"], "hex": frame.hex(), "fields": base["fields"],
                    "note": note})
    return out


def _stream(frames: dict, bad: dict) -> dict:
    parts = [
        ("garbage", bytes([0x00, 0x13, 0xFD, 0x02, 0xFF])),
        ("frame", bytes.fromhex(frames["heartbeat_px4_mission_armed"]["hex"])),
        ("bad", bytes.fromhex(bad["bad_crc"])),
        ("frame", bytes.fromhex(frames["command_ack_accepted_ardupilot"]["hex"])),
        ("bad", bytes.fromhex(bad["signed"])),
        ("frame", bytes.fromhex(frames["mission_request_ardupilot"]["hex"])),
        ("bad", bytes.fromhex(bad["unknown_message"])),
        ("frame", bytes.fromhex(frames["param_value_px4_int32_negative_bytewise"]["hex"])),
        ("bad", bytes.fromhex(bad["truncated"])),
        ("frame", bytes.fromhex(frames["statustext_prearm"]["hex"])),
        ("frame", bytes.fromhex(frames["mission_current_seq_only"]["hex"])),
    ]
    names = {f["hex"]: n for n, f in frames.items()}
    return {
        "hex": b"".join(p for _, p in parts).hex(),
        "decodes": [names[p.hex()] for kind, p in parts if kind == "frame"],
        "note": "Garbage, a bad checksum, a signed frame, an unknown message and a truncated frame sit between "
                "good frames. After any frame it cannot accept the parser resumes its search at the byte after "
                "that 0xFD (never skipping a length it could not verify), so the truncated frame costs no good "
                "frame.",
    }


def build_frames() -> dict:
    installed = _installed_pymavlink()
    if installed != PYMAVLINK_VERSION:
        raise RuntimeError("pymavlink %s is installed; the vectors pin %s" % (installed, PYMAVLINK_VERSION))
    frames = {}
    for spec in _FRAMES:
        record = _frame_record(*spec)
        frames[record["name"]] = record
    covered = {f["msg"] for f in frames.values()}
    missing = set(SUBSET) - covered
    if missing:
        raise RuntimeError("no frame for %s" % sorted(missing))
    bad = _bad_frames(frames)
    return {
        "v": 1,
        "generator": GENERATOR,
        "pymavlink": PYMAVLINK_VERSION,
        "dialect": "common",
        "format": [
            "Every frame is MAVLink 2 (start byte 0xFD), unsigned, made by pymavlink from 'fields'.",
            "A codec encodes 'fields' with the given sysid, compid and seq and must produce 'hex' byte for "
            "byte, including the MAVLink 2 truncation of trailing zero payload bytes (never below 1 byte).",
            "A decoder must turn 'hex' into 'fields'. Missing payload bytes decode as zero.",
            "Field values: integers as JSON numbers; char arrays as strings without the NUL padding; float "
            "fields as the double value of the float32, or \"NaN\"; 'float_bits' holds the exact IEEE 754 "
            "bits (big-endian hex) of every float field and wins over the number.",
            "PARAM_VALUE frames carry 'value': byte-wise (PX4) reads the float bits as an int32; C cast "
            "(ArduPilot) reads the float as a number.",
            "'layouts' gives each subset message in wire order with its CRC_EXTRA; 'extension' fields are "
            "MAVLink 2 extensions, outside the CRC_EXTRA calculation and after the base fields.",
            "'bad_frames' are frames a decoder must skip; 'stream' is one buffer whose good frames must decode "
            "in the order of 'decodes'.",
            "'extended_frames' carry more payload than the definitions here know (newer extension fields); a "
            "decoder must accept them, decode the listed fields and ignore the extra bytes. They are decode-only.",
        ],
        "subset": list(SUBSET),
        "added_to_plan_subset": list(ADDED_TO_PLAN_SUBSET),
        "layouts": _layouts(),
        "frames": list(frames.values()),
        "bad_frames": bad,
        "extended_frames": _extended_frames(frames),
        "stream": _stream(frames, {b["name"]: b["hex"] for b in bad}),
    }


# --------------------------------------------------------------------------------------------------------
# mavlink_transcripts.json
# --------------------------------------------------------------------------------------------------------

AP = "ardupilot"
PX4 = "px4"
SHORT = {AP: "ap", PX4: "px4", "other": "other"}

SOURCES = {
    "AP463": {
        "name": "ArduCopter 4.6.3",
        "commit": "92b0cd788ec29406f26c6f9c31d5ceedbd1cc538",
        "url": "https://github.com/ArduPilot/ardupilot/blob/92b0cd788ec29406f26c6f9c31d5ceedbd1cc538/",
    },
    "AP473": {
        "name": "ArduCopter 4.7.3",
        "commit": "d9c5097c3d8333435712ffed86bca8ce00c4bb46",
        "url": "https://github.com/ArduPilot/ardupilot/blob/d9c5097c3d8333435712ffed86bca8ce00c4bb46/",
    },
    "PX4": {
        "name": "PX4 1.17.0",
        "commit": "d6f12ad1c4f70ad3230afd7d86e971421e02fef4",
        "url": "https://github.com/PX4/PX4-Autopilot/blob/d6f12ad1c4f70ad3230afd7d86e971421e02fef4/",
    },
    "MAVLINK": {"name": "MAVLink developer guide", "url": "https://mavlink.io/en/"},
    "PLAN": {"name": "FlightRisk phone-only mission plan v1 (persisted by task W1-3 as "
                     "docs/design/phone-mission/spec.md)", "url": ""},
}

FIRMWARE = {
    AP: ["ArduCopter 4.6.3", "ArduCopter 4.7.3"],
    PX4: ["PX4 1.17.0"],
    "other": [],
}

MULTIROTOR_TYPES = {2: "QUADROTOR", 3: "COAXIAL", 13: "HEXAROTOR", 14: "OCTOROTOR", 15: "TRICOPTER",
                    29: "DODECAROTOR", 35: "DECAROTOR"}

MODES = {
    AP: [
        (0, "STABILIZE", "other"), (1, "ACRO", "other"), (2, "ALT_HOLD", "other"), (3, "AUTO", "mission"),
        (4, "GUIDED", "hold"), (5, "LOITER", "other"), (6, "RTL", "return"), (7, "CIRCLE", "other"),
        (9, "LAND", "land"), (16, "POSHOLD", "other"), (17, "BRAKE", "other"), (20, "GUIDED_NOGPS", "other"),
        (21, "SMART_RTL", "return"), (27, "AUTO_RTL", "return"),
    ],
    PX4: [
        (_px4_mode(1), "MANUAL", "other"), (_px4_mode(2), "ALTCTL", "other"), (_px4_mode(3), "POSCTL", "other"),
        (_px4_mode(4, 1), "AUTO.READY", "other"), (_px4_mode(4, 2), "AUTO.TAKEOFF", "takeoff"),
        (_px4_mode(4, 3), "AUTO.LOITER", "hold"), (_px4_mode(4, 4), "AUTO.MISSION", "mission"),
        (_px4_mode(4, 5), "AUTO.RTL", "return"), (_px4_mode(4, 6), "AUTO.LAND", "land"),
        (_px4_mode(4, 9), "AUTO.PRECLAND", "land"), (_px4_mode(5), "ACRO", "other"),
        (_px4_mode(6), "OFFBOARD", "other"), (_px4_mode(7), "STABILIZED", "other"),
    ],
}
MODE_BY_NAME = {ap: {name: (cm, role) for cm, name, role in rows} for ap, rows in MODES.items()}

# MAV_CMD_DO_SET_MODE parameters (param1 base mode, param2, param3) per role the phone commands.
SET_MODE = {
    AP: {"hold": (1, 4, 0), "mission": (1, 3, 0), "land": (1, 9, 0)},
    PX4: {"hold": (1, 4, 3), "mission": (1, 4, 4), "land": (1, 4, 6)},
}

STREAMS = [(33, 200000), (1, 1000000), (147, 1000000), (24, 1000000), (245, 1000000), (42, 1000000)]

TIMING = {
    "tries": 3,
    "retry_ms": 1000,
    "heartbeat_ms": 1000,
    "link_loss_ms": 3000,
    "mission_idle_ms": 3000,
    "tolerance_ms": 50,
}

CMD = {"ARM": 400, "SET_MODE": 176, "MISSION_START": 300, "RTL": 20, "LAND": 21, "INTERVAL": 511,
       "REQUEST": 512}
ACCEPTED, TEMP_REJECTED, DENIED, UNSUPPORTED, FAILED = 0, 1, 2, 3, 4

HOME = {"lat_e7": 406894000, "lon_e7": -1117310000}
MISSION = [
    {"kind": "takeoff", "lat_e7": 406894000, "lon_e7": -1117310000, "alt_cm": 1000},
    {"kind": "speed", "cm_s": 300},
    {"kind": "waypoint", "lat_e7": 406894450, "lon_e7": -1117310000, "alt_cm": 1000},
    {"kind": "waypoint", "lat_e7": 406894450, "lon_e7": -1117306000, "alt_cm": 1000},
    {"kind": "waypoint", "lat_e7": 406893550, "lon_e7": -1117306000, "alt_cm": 1000},
    {"kind": "return"},
]
SHORT_MISSION = [MISSION[0], MISSION[2], MISSION[5]]


def _hb(ap: str, mode: str, *, armed: bool = False, flying: bool = False, autopilot=None, mav_type: int = 2):
    custom, _ = MODE_BY_NAME[ap][mode]
    if ap == AP:
        base = 81 | (8 if flying else 0) | (128 if armed else 0)
        status = 4 if flying else 3
    else:
        base = 1 | (28 if mode.startswith("AUTO") else 80) | (128 if armed else 0)
        status = 4 if armed else 3
    return dict(type=mav_type, autopilot=autopilot if autopilot is not None else (3 if ap == AP else 12),
                base_mode=base, custom_mode=custom, system_status=status, mavlink_version=3)


def V(t, msg, src=None, **fields):
    step = {"t": t, "from": "vehicle", "msg": msg, "fields": fields}
    if src:
        step["src"] = src
    return step


def P(t, msg, **fields):
    return {"t": t, "from": "phone", "msg": msg, "fields": fields}


def Q(t, expect, what="snapshot"):
    return {"t": t, "query": what, "expect": expect}


def HB(t, ap, mode, **kw):
    return V(t, "HEARTBEAT", **_hb(ap, mode, **kw))


def GCS_HB(t):
    return P(t, "HEARTBEAT", type=6, autopilot=8, base_mode=0, custom_mode=0, system_status=4, mavlink_version=3)


def CMD_P(t, command, conf=0, *params):
    fields = dict(target_system=1, target_component=1, command=command, confirmation=conf)
    for i, value in enumerate(params, start=1):
        if value:
            fields["param%d" % i] = float(value)
    return P(t, "COMMAND_LONG", **fields)


def ACK(t, command, result=ACCEPTED, target=(255, 190)):
    return V(t, "COMMAND_ACK", command=command, result=result, target_system=target[0], target_component=target[1])


def SET_MODE_P(t, ap, role, conf=0):
    return CMD_P(t, CMD["SET_MODE"], conf, *SET_MODE[ap][role])


def RES(t, outcome, **data):
    return {"t": t, "outcome": outcome, "data": data}


def _item_fields(seq, item):
    base = dict(target_system=1, target_component=1, seq=seq, current=0, autocontinue=1, mission_type=0)
    kind = item["kind"]
    if kind == "home":
        base.update(frame=0, command=16, x=item["lat_e7"], y=item["lon_e7"], z=0.0)
    elif kind in ("takeoff", "waypoint"):
        base.update(frame=3, command=22 if kind == "takeoff" else 16, param4="NaN", x=item["lat_e7"],
                    y=item["lon_e7"], z=item["alt_cm"] / 100.0)
    elif kind == "speed":
        base.update(frame=2, command=178, param1=1.0, param2=item["cm_s"] / 100.0, param3=-1.0)
    elif kind == "return":
        base.update(frame=2, command=20)
    else:
        raise ValueError(kind)
    return base


def _wire_items(ap, items):
    out = list(items)
    if ap == AP:
        out = [{"kind": "home", **HOME}] + out
    return out


def _request(t, ap, seq):
    msg = "MISSION_REQUEST" if ap == AP else "MISSION_REQUEST_INT"
    return V(t, msg, target_system=255, target_component=190, seq=seq, mission_type=0)


def _mission_ack(t, ap, result=0):
    return V(t, "MISSION_ACK", target_system=255, target_component=190, type=result, mission_type=0)


def _param_value(t, name, value, ptype, ap):
    if ap == PX4 and ptype in (1, 2, 3, 4, 5, 6):
        fv = _int32_as_float(int(value))
        return V(t, "PARAM_VALUE", param_id=name, param_value=fv, param_type=ptype, param_count=1027,
                 param_index=611)
    return V(t, "PARAM_VALUE", param_id=name, param_value=float(value), param_type=ptype, param_count=1250,
             param_index=65535)


def _read_p(t, name):
    return P(t, "PARAM_REQUEST_READ", target_system=1, target_component=1, param_id=name, param_index=-1)


def _connected(ap, mode, **kw):
    return {"connected": True, "heartbeat": _hb(ap, mode, **kw)}


BEHAVIOURS = [
    ("MAV-01", "gcs_heartbeat", "2.4 link loss",
     "The phone sends a GCS HEARTBEAT (sysid 255, compid 190, type GCS, autopilot INVALID) every second; in "
     "host mode it sends first, before it hears the vehicle."),
    ("MAV-02", "listen_mode", "1.1 Wi-Fi topology",
     "In listen mode the phone sends nothing until a vehicle packet arrives, then answers its source and keeps "
     "the 1 s heartbeat."),
    ("MAV-03", "connect", "2.6",
     "Connect waits for an autopilot HEARTBEAT and records its sysid, compid, autopilot and type. GCS "
     "heartbeats, components that are not autopilots and later systems are ignored."),
    ("MAV-04", "refuse_vehicle", "1.1",
     "Connect refuses an autopilot other than ArduPilot (3) or PX4 (12), and a vehicle type that is not a "
     "multirotor, and then sends nothing more."),
    ("MAV-05", "message_intervals", "2.6",
     "The phone requests message intervals with MAV_CMD_SET_MESSAGE_INTERVAL, one command at a time; a refused "
     "interval is reported, not fatal."),
    ("MAV-06", "home_on_request", "2.6",
     "HOME_POSITION is requested with MAV_CMD_REQUEST_MESSAGE and the request resolves on the first "
     "HOME_POSITION after it, whether it comes before or after the ACK."),
    ("MAV-07", "home_unset", "2.6",
     "Without a home the request is refused (PX4) or accepted and left unanswered (ArduPilot), which ends in a "
     "timeout after three tries."),
    ("MAV-08", "autopilot_version", "2.6",
     "AUTOPILOT_VERSION is requested once and flight_sw_version is decoded as major.minor.patch."),
    ("MAV-09", "command_retry", "2.6",
     "COMMAND_LONG is resent after 1 s without an ACK, with confirmation raised by one, up to three tries."),
    ("MAV-10", "command_timeout", "2.6",
     "Three tries without an ACK end in a timeout 1 s after the last try; no fourth try is sent."),
    ("MAV-11", "command_refused", "2.6",
     "Any ACK result other than ACCEPTED ends the command as refused at once; the phone does not retry it."),
    ("MAV-12", "command_ack_match", "2.6",
     "An ACK counts only when its command matches and it is addressed to the phone (or to system 0)."),
    ("MAV-13", "param_read", "2.6",
     "Parameters are read by name; integer values decode byte-wise on PX4 and by C cast on ArduPilot."),
    ("MAV-14", "param_retry", "2.6",
     "A parameter read is resent after 1 s without its PARAM_VALUE, up to three tries; values for other names "
     "are ignored."),
    ("MAV-15", "param_absent", "2.6",
     "A name the vehicle does not know ends as refused on PARAM_ERROR (ArduCopter 4.7) or as a timeout after "
     "three silent tries (ArduCopter 4.6, PX4); the preflight counts either as unreadable."),
    ("MAV-16", "param_alias", "2.4 GCS failsafe",
     "A read with several names tries them in order and returns the first that answers, so a renamed "
     "parameter (ArduCopter 4.7) is still read."),
    ("MAV-17", "mission_upload", "2.6",
     "Missions are uploaded with MISSION_COUNT, the vehicle's requests and MISSION_ITEM_INT replies, ending in "
     "MISSION_ACK ACCEPTED. ArduPilot gets a home placeholder at sequence 0 and requests with MISSION_REQUEST; "
     "PX4 does not and requests with MISSION_REQUEST_INT."),
    ("MAV-18", "mission_rerequest", "2.6",
     "MISSION_COUNT is resent after 1 s without a request; a repeated request is answered with the same item."),
    ("MAV-19", "mission_refused", "2.6",
     "A MISSION_ACK with an error ends the upload as refused; no more items are sent."),
    ("MAV-20", "mission_timeout", "2.6",
     "Three unanswered MISSION_COUNT tries, or 3 s without a request during an upload, end in a timeout."),
    ("MAV-21", "start", "2.6",
     "Start sets the hold mode, arms, then sends MAV_CMD_MISSION_START (0, 0); each step waits for its ACK."),
    ("MAV-22", "start_refused", "2.6",
     "A refused step ends the start as refused at that step; the later steps are never sent."),
    ("MAV-23", "hold", "2.6", "Hold sets ArduCopter GUIDED or PX4 AUTO.LOITER with MAV_CMD_DO_SET_MODE."),
    ("MAV-24", "resume", "2.6", "Resume sets ArduCopter AUTO or PX4 AUTO.MISSION with MAV_CMD_DO_SET_MODE."),
    ("MAV-25", "return", "2.6", "Return sends MAV_CMD_NAV_RETURN_TO_LAUNCH."),
    ("MAV-26", "land", "2.6, 2.4 Land all", "Land sends MAV_CMD_NAV_LAND."),
    ("MAV-27", "land_escalation", "2.4 Land all",
     "MAV_CMD_NAV_LAND is tried up to three times; if it is not accepted, MAV_CMD_DO_SET_MODE LAND is tried up "
     "to three times."),
    ("MAV-28", "telemetry", "2.2 Telemetry",
     "Telemetry messages fill one snapshot with raw units and the time of each reading; unknown values (-1, "
     "UINT16_MAX, 255) never overwrite a reading."),
    ("MAV-29", "mode_report", "2.2 R8, R12",
     "The snapshot reports the mode, its role and whether the phone commanded it; heartbeats that arrive while "
     "a mode command waits for its ACK are not judged."),
    ("MAV-30", "mission_progress", "2.6",
     "MISSION_CURRENT is mapped to the plan's item index (ArduPilot sequence minus 1 for the home placeholder)."),
    ("MAV-31", "link_liveness", "2.2 R7, 2.4 Land all",
     "The link is up while vehicle packets arrive and down after 3 s without one; the control link stays true "
     "once any packet has arrived."),
]
BEHAVIOUR_IDS = [b[0] for b in BEHAVIOURS]

# Citations shared by several transcripts.
C_HB = {
    AP: ["AP463 libraries/GCS_MAVLink/GCS_Common.cpp:4149-4155", "AP463 ArduCopter/events.cpp:124-160",
         "AP473 libraries/GCS_MAVLink/GCS_Common.cpp:4338-4344", "AP473 libraries/GCS_MAVLink/GCS.cpp:45-59"],
    PX4: ["PX4 src/modules/mavlink/mavlink_receiver.cpp:2152-2165", "PX4 src/modules/commander/Commander.cpp:2845-2858",
          "PX4 src/modules/mavlink/mavlink_main.cpp:2696-2701"],
}
C_CMD = {
    AP: ["AP463 libraries/GCS_MAVLink/GCS_Common.cpp:5170-5200", "MAVLINK services/command.html"],
    PX4: ["PX4 src/modules/mavlink/mavlink_receiver.cpp:439-458", "PX4 src/modules/mavlink/mavlink_receiver.cpp:122-134",
          "PX4 src/modules/commander/Commander.cpp:2674-2680", "MAVLINK services/command.html"],
}
C_SETMODE = {
    AP: ["AP463 libraries/GCS_MAVLink/GCS_Common.cpp:4872-4878", "AP463 libraries/GCS_MAVLink/GCS_Common.cpp:2826-2838",
         "AP463 ArduCopter/mode.h:75-105"],
    PX4: ["PX4 src/modules/commander/Commander.cpp:800-908", "PX4 src/modules/commander/px4_custom_mode.h:46-98"],
}
C_MISSION = {
    AP: ["AP463 libraries/GCS_MAVLink/MissionItemProtocol.cpp:372", "AP473 libraries/GCS_MAVLink/MissionItemProtocol.cpp:370",
         "AP463 libraries/GCS_MAVLink/GCS_Common.cpp:4512-4515", "AP463 libraries/GCS_MAVLink/MissionItemProtocol.cpp:289-299",
         "AP463 libraries/AP_Mission/AP_Mission.cpp:816-821", "AP463 libraries/AP_Mission/AP_Mission.cpp:1465-1490",
         "MAVLINK services/mission.html"],
    PX4: ["PX4 src/modules/mavlink/mavlink_mission.cpp:445-470", "PX4 src/modules/mavlink/mavlink_mission.h:117",
          "PX4 src/modules/mavlink/mavlink_mission.cpp:996-1035", "PX4 src/modules/mavlink/mavlink_mission.cpp:1397-1402",
          "PX4 src/modules/mavlink/mavlink_mission.cpp:1550", "PX4 src/modules/mavlink/mavlink_mission.cpp:1602-1630",
          "MAVLINK services/mission.html"],
}
C_PARAM = {
    AP: ["AP463 libraries/GCS_MAVLink/GCS_MAVLink.cpp:101-114", "AP463 libraries/AP_Param/AP_Param.cpp:1946-1960",
         "MAVLINK services/parameter.html"],
    PX4: ["PX4 src/modules/mavlink/mavlink_parameters.cpp:492-500", "PX4 src/modules/mavlink/mavlink_parameters.cpp:531-545",
          "PX4 src/modules/mavlink/mavlink_main.cpp:1054-1066", "MAVLINK services/parameter.html"],
}


def _tr(behaviour, ap, title, encodes, cites, op, steps, result, end, *, setup=None, variant="", heartbeats="ignore",
        firmware=None):
    tid = "%s.%s%s" % (behaviour, SHORT[ap], ("." + variant) if variant else "")
    return {
        "id": tid,
        "behaviour": behaviour,
        "autopilot": ap,
        "firmware": firmware if firmware is not None else FIRMWARE[ap],
        "title": title,
        "encodes": encodes,
        "cites": cites,
        "vehicle": {"sysid": 1, "compid": 1},
        "setup": setup or {"connected": False},
        "heartbeats": heartbeats,
        "op": op,
        "steps": steps,
        "result": result,
        "end_ms": end,
    }


def _transcripts() -> list:
    out = []
    add = out.append
    ap_or_px4 = (AP, PX4)

    # MAV-01 GCS heartbeat, host mode.
    for ap in ap_or_px4:
        mode = "STABILIZE" if ap == AP else "POSCTL"
        add(_tr("MAV-01", ap, "GCS heartbeat at 1 Hz in host mode",
                ("ArduCopter resets its GCS failsafe timer only on a HEARTBEAT whose sysid is SYSID_MYGCS (4.6, "
                 "default 255) or within MAV_GCS_SYSID..MAV_GCS_SYSID_HI (4.7), whatever its type, and its GCS "
                 "failsafe arms only after the first such heartbeat." if ap == AP else
                 "PX4 counts any HEARTBEAT of type MAV_TYPE_GCS, from any sysid, as the GCS link; data-link loss "
                 "triggers after COM_DL_LOSS_T without one, only once one was seen, and PX4 sends COMMAND_ACK only "
                 "to components it has heard from.") +
                " The phone therefore sends sysid 255 with type GCS, first and every second.",
                C_HB[ap] + ["MAVLINK services/heartbeat.html"],
                {"name": "connect", "args": {"transport": "host"}},
                [GCS_HB(0), HB(400, ap, mode), GCS_HB(1000), GCS_HB(2000)],
                RES(400, "ok", sysid=1, compid=1, autopilot=ap, mav_type=2), 2500, heartbeats="match"))

    # MAV-02 listen mode.
    for ap in ap_or_px4:
        mode = "STABILIZE" if ap == AP else "POSCTL"
        add(_tr("MAV-02", ap, "Listen mode answers the first vehicle packet",
                "A bridge in listen mode sends the vehicle's stream first; the phone replies to the packet's source "
                "address and from then on heartbeats every second, which the vehicle needs before its GCS "
                "failsafe and command ACKs work (see MAV-01).",
                C_HB[ap] + ["PLAN 1.1"],
                {"name": "connect", "args": {"transport": "listen"}},
                [HB(700, ap, mode), GCS_HB(700), GCS_HB(1700), GCS_HB(2700)],
                RES(700, "ok", sysid=1, compid=1, autopilot=ap, mav_type=2), 3000, heartbeats="match"))

    # MAV-03 connect.
    for ap in ap_or_px4:
        mode = "STABILIZE" if ap == AP else "POSCTL"
        cites = (["AP463 libraries/GCS_MAVLink/GCS_Common.cpp:3092-3101", "AP463 ArduCopter/GCS_Mavlink.cpp:7-27",
                  "AP463 libraries/GCS_MAVLink/GCS_MAVLink.cpp:79"] if ap == AP else
                 ["PX4 src/modules/mavlink/streams/HEARTBEAT.hpp:129", "PX4 ROMFS/px4fmu_common/init.d/rc.mc_defaults:10-11",
                  "PX4 src/modules/mavlink/mavlink_params.c:41-50"])
        add(_tr("MAV-03", ap, "Connect identifies the autopilot and ignores other senders",
                ("ArduCopter's autopilot heartbeat comes from compid 1 with autopilot 3 (ARDUPILOTMEGA) and type "
                 "QUADROTOR (from the frame)." if ap == AP else
                 "PX4's heartbeat comes from MAV_COMP_ID (default 1) with autopilot 12 (PX4) and type from MAV_TYPE "
                 "(2 for multicopter airframes).") +
                " A camera component (autopilot INVALID), a second GCS and a second vehicle on the same network "
                "are not the vehicle.",
                cites + ["MAVLINK services/heartbeat.html"],
                {"name": "connect", "args": {"transport": "host"}},
                [V(100, "HEARTBEAT", src={"sysid": 1, "compid": 100}, type=30, autopilot=8, base_mode=0,
                   custom_mode=0, system_status=4, mavlink_version=3),
                 V(200, "HEARTBEAT", src={"sysid": 200, "compid": 190}, type=6, autopilot=8, base_mode=0,
                   custom_mode=0, system_status=4, mavlink_version=3),
                 HB(300, ap, mode),
                 V(1300, "HEARTBEAT", src={"sysid": 2, "compid": 1}, **_hb(ap, mode)),
                 Q(1400, {"sysid": 1, "compid": 1, "autopilot": ap})],
                RES(300, "ok", sysid=1, compid=1, autopilot=ap, mav_type=2), 2000))

    # MAV-04 refuse vehicle.
    refusals = [
        (AP, 3, 1, "unsupported_vehicle_type",
         "An ArduPlane heartbeat (autopilot 3, type FIXED_WING) uses Plane mode numbers, so Copter's GUIDED (4) "
         "would select a different Plane mode; v1 flies multirotors only.",
         ["AP463 ArduCopter/mode.h:75-105", "MAVLINK messages/common.html#MAV_TYPE"]),
        (PX4, 12, 22, "unsupported_vehicle_type",
         "A PX4 VTOL (type 22) can be in fixed-wing flight, where hold and land behave differently; v1 flies "
         "multirotors only.",
         ["PX4 src/modules/commander/px4_custom_mode.h:46-98", "MAVLINK messages/common.html#MAV_TYPE"]),
        ("other", 0, 2, "unsupported_autopilot",
         "An autopilot that is neither ArduPilot (3) nor PX4 (12), here MAV_AUTOPILOT_GENERIC, has unknown mode "
         "numbers and is refused.",
         ["PLAN 1.1", "MAVLINK messages/common.html#MAV_AUTOPILOT"]),
    ]
    for ap, autopilot, mav_type, reason, encodes, cites in refusals:
        hb = dict(type=mav_type, autopilot=autopilot, base_mode=81, custom_mode=0, system_status=3, mavlink_version=3)
        add(_tr("MAV-04", ap, "Connect refuses an unsupported vehicle", encodes + " After refusing, the phone sends "
                "nothing more on that link, not even heartbeats.", cites,
                {"name": "connect", "args": {"transport": "host"}},
                [GCS_HB(0), V(300, "HEARTBEAT", **hb)],
                RES(300, "refused", reason=reason, autopilot=autopilot, mav_type=mav_type), 2500, heartbeats="match"))

    # MAV-05 message intervals.
    for ap in ap_or_px4:
        steps, t = [], 0
        refused = []
        for msgid, interval in STREAMS:
            steps.append(CMD_P(t, CMD["INTERVAL"], 0, msgid, interval))
            result = FAILED if (ap == PX4 and msgid == 42) else ACCEPTED
            if result:
                refused.append(msgid)
            t += 40
            steps.append(ACK(t, CMD["INTERVAL"], result))
        accepted = [m for m, _ in STREAMS if m not in refused]
        encodes = ("ArduCopter accepts SET_MESSAGE_INTERVAL for every subset message (params 3 to 7 must be 0, "
                   "else DENIED); EXTENDED_SYS_STATE is in no ArduCopter stream, so it arrives only because it is "
                   "requested." if ap == AP else
                   "PX4 accepts SET_MESSAGE_INTERVAL for messages it has a stream for and answers FAILED for "
                   "MISSION_CURRENT, which its mission manager sends at 1 Hz without a stream.")
        cites = (["AP463 libraries/GCS_MAVLink/GCS_Common.cpp:3120-3135", "AP463 libraries/GCS_MAVLink/GCS_Common.cpp:1121",
                  "AP473 libraries/GCS_MAVLink/GCS_Common.cpp:3186-3224"] if ap == AP else
                 ["PX4 src/modules/mavlink/mavlink_receiver.cpp:584-588", "PX4 src/modules/mavlink/mavlink_receiver.cpp:2245-2290",
                  "PX4 src/modules/mavlink/mavlink_mission.cpp:506-510"])
        add(_tr("MAV-05", ap, "Request message intervals one at a time",
                encodes + " COMMAND_ACK names only the command, not param1, so the phone keeps one SET_MESSAGE_INTERVAL "
                "outstanding at a time. MISSION_CURRENT is added to the plan's list for lane progress.",
                cites + ["MAVLINK messages/common.html#MAV_CMD_SET_MESSAGE_INTERVAL"],
                {"name": "request_streams", "args": {}}, steps,
                RES(t, "ok", accepted=accepted, refused=refused), t + 2000, setup=_connected(ap, "STABILIZE" if ap == AP else "POSCTL")))

    home_fields = dict(latitude=406894000, longitude=-1117310000, altitude=1600000, q=[1.0, 0.0, 0.0, 0.0])
    # MAV-06 home on request.
    for ap in ap_or_px4:
        if ap == AP:
            steps = [CMD_P(0, CMD["REQUEST"], 0, 242), ACK(30, CMD["REQUEST"]), V(60, "HOME_POSITION", **home_fields)]
            t, encodes = 60, ("ArduCopter queues the requested message and ACKs ACCEPTED at once, so HOME_POSITION "
                              "arrives after the ACK.")
            cites = ["AP463 libraries/GCS_MAVLink/GCS_Common.cpp:3203-3212", "AP463 libraries/GCS_MAVLink/GCS_Common.cpp:3026-3030"]
        else:
            steps = [CMD_P(0, CMD["REQUEST"], 0, 242), V(30, "HOME_POSITION", **home_fields), ACK(31, CMD["REQUEST"])]
            t, encodes = 30, "PX4 sends the requested message inside the command handler, so HOME_POSITION arrives before the ACK."
            cites = ["PX4 src/modules/mavlink/mavlink_receiver.cpp:603-606", "PX4 src/modules/mavlink/mavlink_receiver.cpp:726-752",
                     "PX4 src/modules/mavlink/streams/HOME_POSITION.hpp:60-92"]
        add(_tr("MAV-06", ap, "Request HOME_POSITION", encodes + " The request resolves on the HOME_POSITION; a later "
                "ACK is ignored.", cites + ["MAVLINK messages/common.html#MAV_CMD_REQUEST_MESSAGE"],
                {"name": "request_home", "args": {}}, steps,
                RES(t, "ok", lat_e7=406894000, lon_e7=-1117310000, alt_mm=1600000), t + 1500,
                setup=_connected(ap, "STABILIZE" if ap == AP else "POSCTL")))

    # MAV-07 home unset.
    add(_tr("MAV-07", AP, "HOME_POSITION requested before home is set",
            "ArduCopter ACKs MAV_CMD_REQUEST_MESSAGE for HOME_POSITION with ACCEPTED but sends nothing while home is "
            "unset, so each try waits 1 s for the message and the request times out after three tries.",
            ["AP463 libraries/GCS_MAVLink/GCS_Common.cpp:3203-3212", "AP463 libraries/GCS_MAVLink/GCS_Common.cpp:3026-3030"],
            {"name": "request_home", "args": {}},
            [CMD_P(0, CMD["REQUEST"], 0, 242), ACK(30, CMD["REQUEST"]), CMD_P(1000, CMD["REQUEST"], 1, 242),
             ACK(1030, CMD["REQUEST"]), CMD_P(2000, CMD["REQUEST"], 2, 242), ACK(2030, CMD["REQUEST"])],
            RES(3000, "timeout"), 4500, setup=_connected(AP, "STABILIZE")))
    add(_tr("MAV-07", PX4, "HOME_POSITION requested before home is set",
            "PX4 answers MAV_CMD_REQUEST_MESSAGE for HOME_POSITION with DENIED while home is not valid; the request "
            "ends as refused at once and the caller asks again later.",
            ["PX4 src/modules/mavlink/streams/HOME_POSITION.hpp:60-92", "PX4 src/modules/mavlink/mavlink_receiver.cpp:726-752"],
            {"name": "request_home", "args": {}},
            [CMD_P(0, CMD["REQUEST"], 0, 242), ACK(30, CMD["REQUEST"], DENIED)],
            RES(30, "refused", mav_result=DENIED), 3000, setup=_connected(PX4, "POSCTL")))

    # MAV-08 autopilot version.
    versions = {
        AP: (dict(capabilities=0xFDEF, flight_sw_version=0x040603FF,
                  flight_custom_version=[0x39, 0x32, 0x62, 0x30, 0x63, 0x64, 0x37, 0x38]), "4.6.3", True,
             ["AP463 libraries/GCS_MAVLink/GCS_Common.cpp:2906-2954", "AP463 libraries/GCS_MAVLink/GCS_Common.cpp:6976-7006"]),
        PX4: (dict(capabilities=0x3E3F, flight_sw_version=0x011100FF, middleware_sw_version=0x011100FF), "1.17.0",
              False, ["PX4 src/modules/mavlink/mavlink_main.cpp:1054-1066",
                      "PX4 src/modules/mavlink/streams/AUTOPILOT_VERSION.hpp:56-59"]),
    }
    for ap in ap_or_px4:
        fields, text, ack_first, cites = versions[ap]
        msg = V(30 if not ack_first else 60, "AUTOPILOT_VERSION", **fields)
        ack = ACK(30 if ack_first else 31, CMD["REQUEST"])
        steps = [CMD_P(0, CMD["REQUEST"], 0, 148)] + ([ack, msg] if ack_first else [msg, ack])
        add(_tr("MAV-08", ap, "Read the autopilot version",
                "flight_sw_version packs major, minor and patch in bytes 3, 2 and 1 and the release type in byte 0 "
                "(255 is official). The phone logs it and uses it to order renamed parameter names (MAV-16).",
                cites + ["MAVLINK messages/common.html#AUTOPILOT_VERSION"],
                {"name": "read_version", "args": {}}, steps,
                RES(msg["t"], "ok", flight_sw_version=text, release_type=255, capabilities=fields["capabilities"]),
                2000, setup=_connected(ap, "STABILIZE" if ap == AP else "POSCTL")))

    # MAV-09 command retry: ArduPilot return, PX4 hold.
    add(_tr("MAV-09", AP, "Command resent after 1 s without an ACK",
            "ArduCopter never reads COMMAND_LONG.confirmation and runs a repeated command again; the phone only "
            "retries commands whose repeat is harmless (mode changes, return, land, arm when already armed).",
            C_CMD[AP] + ["AP463 ArduCopter/GCS_Mavlink.cpp:825-836"],
            {"name": "return_home", "args": {}},
            [CMD_P(0, CMD["RTL"]), CMD_P(1000, CMD["RTL"], 1), ACK(1040, CMD["RTL"])],
            RES(1040, "ok"), 3500, setup=_connected(AP, "AUTO", armed=True, flying=True)))
    add(_tr("MAV-09", PX4, "Command resent after 1 s without an ACK",
            "PX4 copies confirmation into the vehicle command but does not deduplicate, so a repeated DO_SET_MODE "
            "is applied again, which is harmless.",
            C_CMD[PX4] + ["PX4 src/modules/mavlink/mavlink_receiver.cpp:497"] + C_SETMODE[PX4],
            {"name": "hold", "args": {}},
            [SET_MODE_P(0, PX4, "hold"), SET_MODE_P(1000, PX4, "hold", 1), ACK(1040, CMD["SET_MODE"])],
            RES(1040, "ok"), 3500, setup=_connected(PX4, "AUTO.MISSION", armed=True, flying=True)))

    # MAV-10 command timeout: ArduPilot resume, PX4 return.
    add(_tr("MAV-10", AP, "Command times out after three tries",
            "Without any ACK the phone stops after three tries and reports a timeout 1 s after the last; the "
            "autopilot keeps its mode, and the mission rules decide what follows.",
            C_CMD[AP] + C_SETMODE[AP],
            {"name": "resume", "args": {}},
            [SET_MODE_P(0, AP, "mission"), SET_MODE_P(1000, AP, "mission", 1), SET_MODE_P(2000, AP, "mission", 2)],
            RES(3000, "timeout"), 5000, setup=_connected(AP, "GUIDED", armed=True, flying=True)))
    add(_tr("MAV-10", PX4, "Command times out after three tries",
            "Without any ACK the phone stops after three tries and reports a timeout 1 s after the last.",
            C_CMD[PX4] + ["PX4 src/modules/commander/Commander.cpp:1050-1062"],
            {"name": "return_home", "args": {}},
            [CMD_P(0, CMD["RTL"]), CMD_P(1000, CMD["RTL"], 1), CMD_P(2000, CMD["RTL"], 2)],
            RES(3000, "timeout"), 5000, setup=_connected(PX4, "AUTO.MISSION", armed=True, flying=True)))

    # MAV-11 command refused.
    add(_tr("MAV-11", AP, "Refused command is not retried",
            "ArduCopter answers a refused mode change with MAV_RESULT_FAILED (for example AUTO with no mission). "
            "The phone reports the refusal with the result and sends no further try.",
            C_CMD[AP] + C_SETMODE[AP] + ["AP463 ArduCopter/mode_auto.cpp:23-31"],
            {"name": "resume", "args": {}},
            [SET_MODE_P(0, AP, "mission"), ACK(30, CMD["SET_MODE"], FAILED)],
            RES(30, "refused", mav_result=FAILED), 3000, setup=_connected(AP, "GUIDED", armed=True, flying=True)))
    add(_tr("MAV-11", PX4, "Refused command is not retried",
            "PX4 answers a mode change it refuses with MAV_RESULT_TEMPORARILY_REJECTED. The phone does not retry "
            "it by itself; the caller decides.",
            C_CMD[PX4] + C_SETMODE[PX4],
            {"name": "hold", "args": {}},
            [SET_MODE_P(0, PX4, "hold"), ACK(30, CMD["SET_MODE"], TEMP_REJECTED)],
            RES(30, "refused", mav_result=TEMP_REJECTED), 3000, setup=_connected(PX4, "AUTO.MISSION", armed=True, flying=True)))

    # MAV-12 ACK matching.
    for ap in ap_or_px4:
        add(_tr("MAV-12", ap, "Only the matching ACK addressed to the phone counts",
                ("ArduCopter addresses COMMAND_ACK to the sender's sysid and compid." if ap == AP else
                 "PX4 addresses COMMAND_ACK to the sender's sysid and compid.") +
                " An ACK for another command, or one addressed to another GCS, is not the answer; system 0 is "
                "accepted as a broadcast.",
                C_CMD[ap] + (["AP463 libraries/GCS_MAVLink/GCS_Common.cpp:5189-5191"] if ap == AP else []),
                {"name": "return_home", "args": {}},
                [CMD_P(0, CMD["RTL"]), ACK(20, CMD["SET_MODE"]), ACK(30, CMD["RTL"], FAILED, target=(200, 190)),
                 ACK(40, CMD["RTL"], ACCEPTED, target=(0, 0))],
                RES(40, "ok"), 3000, setup=_connected(ap, "AUTO" if ap == AP else "AUTO.MISSION", armed=True, flying=True)))

    # MAV-13 parameter read.
    param_cases = [
        (AP, "a", "FS_GCS_ENABLE", 1, 2, "ArduCopter sends INT8 FS_GCS_ENABLE as a C cast to float (1.0) with type INT8.",
         ["AP463 ArduCopter/Parameters.h:411", "AP463 ArduCopter/Parameters.cpp:160-165"]),
        (AP, "b", "FS_GCS_TIMEOUT", 5.0, 9, "ArduCopter sends float FS_GCS_TIMEOUT (seconds) as REAL32.",
         ["AP463 ArduCopter/Parameters.cpp:984-991", "AP473 ArduCopter/Parameters.h:642"]),
        (PX4, "a", "NAV_DLL_ACT", 2, 6, "PX4 copies the int32 NAV_DLL_ACT (2, Return) byte-wise into param_value; a "
         "C cast would read about 2.8e-45 and wrongly see 0.", ["PX4 src/modules/commander/commander_params.c:575-598"]),
        (PX4, "b", "COM_DL_LOSS_T", 10, 6, "PX4 copies the int32 COM_DL_LOSS_T (seconds) byte-wise.",
         ["PX4 src/modules/commander/commander_params.c:85-98"]),
    ]
    for ap, variant, name, value, ptype, encodes, extra in param_cases:
        add(_tr("MAV-13", ap, "Read a parameter by name", encodes + " The reply is matched by name, never by index.",
                C_PARAM[ap] + extra, {"name": "read_param", "args": {"names": [name]}},
                [_read_p(0, name), _param_value(40, name, value, ptype, ap)],
                RES(40, "ok", name=name, value=value, type=ptype), 2000,
                setup=_connected(ap, "STABILIZE" if ap == AP else "POSCTL"), variant=variant))

    # MAV-14 parameter retry.
    for ap in ap_or_px4:
        name, other, value, ptype = (("FS_GCS_ENABLE", "FS_GCS_TIMEOUT", 1, 2) if ap == AP else
                                     ("NAV_DLL_ACT", "COM_DL_LOSS_T", 2, 6))
        add(_tr("MAV-14", ap, "Parameter read resent after 1 s",
                ("ArduCopter answers queued reads from its io timer, up to five per pass, so a reply can be late or "
                 "lost on a busy link." if ap == AP else "PX4 answers a read once; a lost reply is not resent.") +
                " PARAM_REQUEST_READ has no confirmation field, so a retry is the same message; a PARAM_VALUE for "
                "another name is ignored.",
                C_PARAM[ap] + (["AP463 libraries/GCS_MAVLink/GCS_Param.cpp:225-261"] if ap == AP else
                               ["PX4 src/modules/mavlink/mavlink_parameters.cpp:176-205"]),
                {"name": "read_param", "args": {"names": [name]}},
                [_read_p(0, name), _param_value(300, other, 5.0 if ap == AP else 10, 9 if ap == AP else 6, ap),
                 _read_p(1000, name), _param_value(1050, name, value, ptype, ap)],
                RES(1050, "ok", name=name, value=value, type=ptype), 3000,
                setup=_connected(ap, "STABILIZE" if ap == AP else "POSCTL")))

    # MAV-15 parameter absent.
    add(_tr("MAV-15", AP, "Unknown parameter name on ArduCopter 4.6",
            "ArduCopter 4.6.3 drops a read of an unknown name without any reply, so the read times out after three "
            "tries. Here the phone asks 4.6.3 for the 4.7 name MAV_GCS_SYSID.",
            C_PARAM[AP] + ["AP463 libraries/GCS_MAVLink/GCS_Param.cpp:391-394"],
            {"name": "read_param", "args": {"names": ["MAV_GCS_SYSID"]}},
            [_read_p(0, "MAV_GCS_SYSID"), _read_p(1000, "MAV_GCS_SYSID"), _read_p(2000, "MAV_GCS_SYSID")],
            RES(3000, "timeout"), 4500, setup=_connected(AP, "STABILIZE"), variant="463",
            firmware=["ArduCopter 4.6.3"]))
    add(_tr("MAV-15", AP, "Unknown parameter name on ArduCopter 4.7",
            "ArduCopter 4.7.3 answers a read of an unknown name with PARAM_ERROR (MAV_PARAM_ERROR_DOES_NOT_EXIST) "
            "addressed to the requester, so the read ends as refused at once. Here the phone asks 4.7.3 for the "
            "4.6 name SYSID_MYGCS, which 4.7 renamed to MAV_GCS_SYSID.",
            C_PARAM[AP] + ["AP473 libraries/GCS_MAVLink/GCS_Param.cpp:405-421", "AP473 libraries/GCS_MAVLink/GCS_Param.cpp:450-466"],
            {"name": "read_param", "args": {"names": ["SYSID_MYGCS"]}},
            [_read_p(0, "SYSID_MYGCS"), V(40, "PARAM_ERROR", target_system=255, target_component=190,
                                         param_id="SYSID_MYGCS", param_index=-1, error=1)],
            RES(40, "refused", param_error=1), 3000, setup=_connected(AP, "STABILIZE"), variant="473",
            firmware=["ArduCopter 4.7.3"]))
    add(_tr("MAV-15", PX4, "Unknown parameter name on PX4",
            "PX4 sends nothing for a read of an unknown name (no PARAM_VALUE, no STATUSTEXT), so the read times "
            "out after three tries. The name is synthetic.",
            C_PARAM[PX4] + ["PX4 src/modules/mavlink/mavlink_parameters.cpp:187-205", "PX4 src/modules/mavlink/mavlink_parameters.cpp:475-478"],
            {"name": "read_param", "args": {"names": ["FR_ABSENT"]}},
            [_read_p(0, "FR_ABSENT"), _read_p(1000, "FR_ABSENT"), _read_p(2000, "FR_ABSENT")],
            RES(3000, "timeout"), 4500, setup=_connected(PX4, "POSCTL")))

    # MAV-16 parameter alias.
    add(_tr("MAV-16", AP, "Renamed GCS sysid parameter on ArduCopter 4.7",
            "ArduCopter 4.7.3 renamed SYSID_MYGCS to MAV_GCS_SYSID (and SYSID_THISMAV to MAV_SYSID). With the 4.6 "
            "name first, PARAM_ERROR moves the read to the next name at once; the value is INT16 255.",
            C_PARAM[AP] + ["AP473 libraries/GCS_MAVLink/GCS.cpp:45-59", "AP473 libraries/GCS_MAVLink/GCS.h:1323",
                           "AP473 ArduCopter/Parameters.cpp:1296-1298", "AP473 libraries/GCS_MAVLink/GCS_Param.cpp:405-421"],
            {"name": "read_param", "args": {"names": ["SYSID_MYGCS", "MAV_GCS_SYSID"]}},
            [_read_p(0, "SYSID_MYGCS"), V(40, "PARAM_ERROR", target_system=255, target_component=190,
                                         param_id="SYSID_MYGCS", param_index=-1, error=1),
             _read_p(40, "MAV_GCS_SYSID"), _param_value(80, "MAV_GCS_SYSID", 255, 4, AP)],
            RES(80, "ok", name="MAV_GCS_SYSID", value=255, type=4), 3000, setup=_connected(AP, "STABILIZE"),
            variant="473", firmware=["ArduCopter 4.7.3"]))
    add(_tr("MAV-16", AP, "GCS sysid parameter on ArduCopter 4.6",
            "ArduCopter 4.6.3 knows SYSID_MYGCS (INT16, default 255); the first name answers and the second is "
            "never asked.",
            C_PARAM[AP] + ["AP463 ArduCopter/Parameters.h:394", "AP463 ArduCopter/Parameters.cpp:46-52"],
            {"name": "read_param", "args": {"names": ["SYSID_MYGCS", "MAV_GCS_SYSID"]}},
            [_read_p(0, "SYSID_MYGCS"), _param_value(40, "SYSID_MYGCS", 255, 4, AP)],
            RES(40, "ok", name="SYSID_MYGCS", value=255, type=4), 3000, setup=_connected(AP, "STABILIZE"),
            variant="463", firmware=["ArduCopter 4.6.3"]))
    add(_tr("MAV-16", PX4, "Name list with a silent first name on PX4",
            "PX4 1.17.0 renames none of the parameters the phone reads, so the first name here is synthetic: PX4 "
            "stays silent for it, the phone moves on after three tries, and MAV_SYS_ID answers byte-wise.",
            C_PARAM[PX4] + ["PX4 src/modules/mavlink/mavlink_parameters.cpp:187-205", "PX4 src/modules/mavlink/mavlink_params.c:41"],
            {"name": "read_param", "args": {"names": ["FR_ABSENT", "MAV_SYS_ID"]}},
            [_read_p(0, "FR_ABSENT"), _read_p(1000, "FR_ABSENT"), _read_p(2000, "FR_ABSENT"),
             _read_p(3000, "MAV_SYS_ID"), _param_value(3040, "MAV_SYS_ID", 1, 6, PX4)],
            RES(3040, "ok", name="MAV_SYS_ID", value=1, type=6), 5000, setup=_connected(PX4, "POSCTL")))

    # MAV-17 mission upload.
    for ap in ap_or_px4:
        wire = _wire_items(ap, MISSION)
        steps = [P(0, "MISSION_COUNT", target_system=1, target_component=1, count=len(wire), mission_type=0)]
        t = 0
        for seq, item in enumerate(wire):
            t += 30
            steps.append(_request(t, ap, seq))
            steps.append(P(t, "MISSION_ITEM_INT", **_item_fields(seq, item)))
        t += 30
        steps.append(_mission_ack(t, ap))
        encodes = ("ArduCopter requests every item with MISSION_REQUEST (never MISSION_REQUEST_INT) and accepts "
                   "MISSION_ITEM_INT in reply. Sequence 0 is stored but always read back as home, so the phone sends "
                   "a placeholder at home. Location items use MAV_FRAME_GLOBAL_RELATIVE_ALT; DO_CHANGE_SPEED and "
                   "NAV_RETURN_TO_LAUNCH carry no location and use MAV_FRAME_MISSION (z must not be NaN)."
                   if ap == AP else
                   "PX4 requests items with MISSION_REQUEST_INT (int mode is its default) and stores sequence 0 as "
                   "the first real item. It accepts NAV_TAKEOFF and NAV_WAYPOINT only in a global frame and "
                   "DO_CHANGE_SPEED and NAV_RETURN_TO_LAUNCH only in MAV_FRAME_MISSION; a waypoint's param4 is the "
                   "yaw in degrees, so NaN (not 0, which is north) keeps the heading behaviour.")
        add(_tr("MAV-17", ap, "Upload a mission", encodes + " The final MISSION_ACK ACCEPTED ends the upload.",
                C_MISSION[ap] + (["AP463 libraries/GCS_MAVLink/MissionItemProtocol.cpp:325-331",
                                  "AP463 libraries/AP_Mission/AP_Mission.cpp:1126-1127",
                                  "AP463 libraries/AP_Mission/AP_Mission.cpp:1197-1201"] if ap == AP else
                                 ["PX4 src/modules/mavlink/mavlink_mission.cpp:1437-1442",
                                  "PX4 src/modules/mavlink/mavlink_mission.cpp:1285-1304"]),
                {"name": "upload_mission", "args": {"home": HOME, "items": MISSION}}, steps,
                RES(t, "ok"), t + 2000, setup=_connected(ap, "STABILIZE" if ap == AP else "POSCTL")))

    # MAV-18 re-requests.
    for ap in ap_or_px4:
        wire = _wire_items(ap, SHORT_MISSION)
        count = dict(target_system=1, target_component=1, count=len(wire), mission_type=0)
        gap = 1000 if ap == AP else 250
        steps = [P(0, "MISSION_COUNT", **count), P(1000, "MISSION_COUNT", **count),
                 _request(1040, ap, 0), P(1040, "MISSION_ITEM_INT", **_item_fields(0, wire[0])),
                 _request(1080, ap, 1), P(1080, "MISSION_ITEM_INT", **_item_fields(1, wire[1])),
                 _request(1080 + gap, ap, 1), P(1080 + gap, "MISSION_ITEM_INT", **_item_fields(1, wire[1]))]
        t = 1080 + gap
        for seq in range(2, len(wire)):
            t += 40
            steps += [_request(t, ap, seq), P(t, "MISSION_ITEM_INT", **_item_fields(seq, wire[seq]))]
        t += 40
        steps.append(_mission_ack(t, ap))
        add(_tr("MAV-18", ap, "MISSION_COUNT resent; a repeated request is answered again",
                ("ArduCopter re-sends the same request when no item arrives for about 1 s." if ap == AP else
                 "PX4 re-sends the same request every 250 ms until the item arrives (5 s protocol timeout).") +
                " The phone answers every request with the item it names and resends MISSION_COUNT after 1 s "
                "without a first request.",
                C_MISSION[ap] + (["AP463 libraries/GCS_MAVLink/MissionItemProtocol.cpp:407-411"] if ap == AP else
                                 ["PX4 src/modules/mavlink/mavlink_mission.cpp:561-581", "PX4 src/modules/mavlink/mavlink_mission.h:80-81"]),
                {"name": "upload_mission", "args": {"home": HOME, "items": SHORT_MISSION}}, steps,
                RES(t, "ok"), t + 2000, setup=_connected(ap, "STABILIZE" if ap == AP else "POSCTL")))

    # MAV-19 mission refused.
    wire = _wire_items(AP, MISSION)
    add(_tr("MAV-19", AP, "Mission refused for lack of space",
            "ArduCopter answers a MISSION_COUNT above its storage with MISSION_ACK NO_SPACE (4) and aborts. "
            "ArduCopter truncates its stored mission to the new count when an upload starts, so a failed upload "
            "leaves no usable mission and the phone must never start after it.",
            C_MISSION[AP] + ["AP463 libraries/GCS_MAVLink/MissionItemProtocol.cpp:91-104"],
            {"name": "upload_mission", "args": {"home": HOME, "items": MISSION}},
            [P(0, "MISSION_COUNT", target_system=1, target_component=1, count=len(wire), mission_type=0),
             _mission_ack(30, AP, 4)],
            RES(30, "refused", mission_result=4), 3000, setup=_connected(AP, "STABILIZE")))
    wire = _wire_items(PX4, SHORT_MISSION)
    add(_tr("MAV-19", PX4, "Mission item refused mid-upload",
            "PX4 answers an item it cannot use with STATUSTEXT and MISSION_ACK (here MAV_MISSION_UNSUPPORTED, 3) and returns to "
            "idle; the phone sends no further item.",
            C_MISSION[PX4] + ["PX4 src/modules/mavlink/mavlink_mission.cpp:1124-1136"],
            {"name": "upload_mission", "args": {"home": HOME, "items": SHORT_MISSION}},
            [P(0, "MISSION_COUNT", target_system=1, target_component=1, count=len(wire), mission_type=0),
             _request(30, PX4, 0), P(30, "MISSION_ITEM_INT", **_item_fields(0, wire[0])),
             _request(60, PX4, 1), P(60, "MISSION_ITEM_INT", **_item_fields(1, wire[1])),
             _mission_ack(90, PX4, 3)],
            RES(90, "refused", mission_result=3), 3000, setup=_connected(PX4, "POSCTL")))

    # MAV-20 mission timeout.
    wire = _wire_items(AP, SHORT_MISSION)
    count = dict(target_system=1, target_component=1, count=len(wire), mission_type=0)
    add(_tr("MAV-20", AP, "MISSION_COUNT never answered",
            "Without any request the phone sends MISSION_COUNT three times, 1 s apart, and times out 1 s after "
            "the last; ArduCopter itself abandons an upload after 8 s.",
            C_MISSION[AP] + ["AP463 libraries/GCS_MAVLink/MissionItemProtocol.cpp:391-404"],
            {"name": "upload_mission", "args": {"home": HOME, "items": SHORT_MISSION}},
            [P(0, "MISSION_COUNT", **count), P(1000, "MISSION_COUNT", **count), P(2000, "MISSION_COUNT", **count)],
            RES(3000, "timeout"), 4500, setup=_connected(AP, "STABILIZE")))
    wire = _wire_items(PX4, SHORT_MISSION)
    add(_tr("MAV-20", PX4, "Vehicle silent in the middle of an upload",
            "PX4 keeps re-requesting every 250 ms while the link works, so 3 s with no request means the link is "
            "gone; the phone times out 3 s after the last request and does not send items unasked.",
            C_MISSION[PX4] + ["PX4 src/modules/mavlink/mavlink_mission.cpp:561-581"],
            {"name": "upload_mission", "args": {"home": HOME, "items": SHORT_MISSION}},
            [P(0, "MISSION_COUNT", target_system=1, target_component=1, count=len(wire), mission_type=0),
             _request(30, PX4, 0), P(30, "MISSION_ITEM_INT", **_item_fields(0, wire[0]))],
            RES(3030, "timeout"), 4500, setup=_connected(PX4, "POSCTL")))

    # MAV-21 start.
    start_cites = {
        AP: ["AP463 ArduCopter/GCS_Mavlink.cpp:958-972", "AP473 ArduCopter/GCS_MAVLink_Copter.cpp:695-709",
             "AP463 ArduCopter/mode_guided.cpp:106-114", "AP463 ArduCopter/mode_auto.cpp:210-216",
             "AP463 ArduCopter/Parameters.cpp:967-972", "AP463 ArduCopter/mode_auto.cpp:23-31",
             "AP463 libraries/GCS_MAVLink/GCS_Common.cpp:5020-5042", "AP463 ArduCopter/motors.cpp:87-133",
             "AP463 ArduCopter/Parameters.cpp:340-346"],
        PX4: ["PX4 src/modules/commander/Commander.cpp:948-982", "PX4 src/modules/commander/Commander.cpp:1127-1153",
              "PX4 src/modules/navigator/navigator_main.cpp:681-686", "PX4 src/modules/commander/ModeUtil/mode_requirements.cpp:100-200",
              "PX4 src/modules/commander/commander_params.c:243", "PX4 src/modules/commander/Commander.cpp:2294-2296"],
    }
    start_text = {
        AP: ("ArduCopter refuses arming in AUTO unless AUTO_OPTIONS bit 0 is set (default 0) but always allows a "
             "GCS to arm in GUIDED, so the phone sets GUIDED first. MAV_CMD_MISSION_START needs param1 and param2 "
             "0 (else DENIED), switches to AUTO, sets auto-armed and starts the mission, whose first item after "
             "home is the takeoff. Armed on the ground without a takeoff, ArduCopter disarms after DISARM_DELAY "
             "(10 s)."),
        PX4: ("PX4 allows arming in AUTO.LOITER, so the phone holds first. MAV_CMD_MISSION_START with param1 0 "
              "sets the mission index to 0, switches to AUTO.MISSION and arms in one step (already armed is "
              "accepted). Armed without a takeoff, PX4 disarms after COM_DISARM_PRFLT (10 s)."),
    }
    for ap in ap_or_px4:
        steps = [SET_MODE_P(0, ap, "hold"), ACK(40, CMD["SET_MODE"]), CMD_P(40, CMD["ARM"], 0, 1),
                 ACK(300, CMD["ARM"]), CMD_P(300, CMD["MISSION_START"]), ACK(340, CMD["MISSION_START"]),
                 HB(1000, ap, "AUTO" if ap == AP else "AUTO.MISSION", armed=True, flying=True),
                 Q(1000, {"armed": True, "role": "mission", "commanded": True})]
        add(_tr("MAV-21", ap, "Start: hold mode, arm, mission start", start_text[ap] + " Each step waits for its "
                "ACK; ArduCopter and PX4 both run a repeated MISSION_START again, which restarts at item 0 and is "
                "harmless during the takeoff.",
                start_cites[ap] + C_CMD[ap], {"name": "start", "args": {}}, steps, RES(340, "ok"), 2000,
                setup=_connected(ap, "STABILIZE" if ap == AP else "POSCTL")))

    # MAV-22 start refused.
    for ap in ap_or_px4:
        arm_result = FAILED if ap == AP else DENIED
        text = "PreArm: Need Position Estimate" if ap == AP else "Preflight Fail: No global position"
        steps = [SET_MODE_P(0, ap, "hold"), ACK(40, CMD["SET_MODE"]), CMD_P(40, CMD["ARM"], 0, 1),
                 V(250, "STATUSTEXT", severity=2, text=text), ACK(260, CMD["ARM"], arm_result),
                 Q(300, [{"severity": 2, "text": text}], "statustext")]
        add(_tr("MAV-22", ap, "Arming refused: no mission start",
                ("ArduCopter answers a failed prearm or arm check with MAV_RESULT_FAILED and explains it in a "
                 "STATUSTEXT." if ap == AP else
                 "PX4 runs its preflight checks for a MAVLink arm request and answers a failure with "
                 "MAV_RESULT_DENIED, explained in a STATUSTEXT.") +
                " The phone stops at the refused step and never sends MISSION_START; the STATUSTEXT is kept for "
                "the Check screen.",
                start_cites[ap] + (["PX4 src/modules/commander/Commander.cpp:549-609"] if ap == PX4 else []),
                {"name": "start", "args": {}}, steps, RES(260, "refused", step="arm", mav_result=arm_result), 3000,
                setup=_connected(ap, "STABILIZE" if ap == AP else "POSCTL"), variant="arm"))
        start_refusal = DENIED if ap == AP else TEMP_REJECTED
        steps = [SET_MODE_P(0, ap, "hold"), ACK(40, CMD["SET_MODE"]), CMD_P(40, CMD["ARM"], 0, 1),
                 ACK(300, CMD["ARM"]), CMD_P(300, CMD["MISSION_START"]), ACK(340, CMD["MISSION_START"], start_refusal)]
        add(_tr("MAV-22", ap, "Mission start refused after arming",
                ("ArduCopter answers MISSION_START with DENIED when param1 or param2 is not 0 and FAILED when AUTO "
                 "cannot start (for example no mission)." if ap == AP else
                 "PX4 answers MISSION_START with DENIED when no valid mission exists and TEMPORARILY_REJECTED when "
                 "the mode change or arming fails.") +
                " The phone reports the refusal and sends nothing more; the autopilot disarms by itself after its "
                "10 s preflight disarm delay.",
                start_cites[ap], {"name": "start", "args": {}}, steps,
                RES(340, "refused", step="start", mav_result=start_refusal), 3000,
                setup=_connected(ap, "STABILIZE" if ap == AP else "POSCTL"), variant="start"))

    # MAV-23 to MAV-26: hold, resume, return, land.
    simple_ops = [
        ("MAV-23", "hold", lambda ap: SET_MODE_P(0, ap, "hold"), CMD["SET_MODE"],
         {AP: ("AUTO", "GUIDED"), PX4: ("AUTO.MISSION", "AUTO.LOITER")}, "hold",
         {AP: "ArduCopter holds in GUIDED: entering GUIDED from AUTO starts velocity control with a zero target, "
              "so the copter brakes and holds. LOITER is avoided because with no RC its throttle input would "
              "descend.",
          PX4: "PX4 holds in AUTO.LOITER (main mode 4, sub mode 3)."},
         {AP: ["AP463 ArduCopter/mode_guided.cpp:36-47", "AP463 ArduCopter/mode_guided.cpp:260-267"], PX4: []}),
        ("MAV-24", "resume", lambda ap: SET_MODE_P(0, ap, "mission"), CMD["SET_MODE"],
         {AP: ("GUIDED", "AUTO"), PX4: ("AUTO.LOITER", "AUTO.MISSION")}, "mission",
         {AP: "ArduCopter resumes the mission at its current command when AUTO is set again (MIS_RESTART 0, "
              "the default).",
          PX4: "PX4 resumes the mission at its current item when AUTO.MISSION is set again."},
         {AP: ["AP463 ArduCopter/mode_auto.cpp:88-95", "AP463 libraries/AP_Mission/AP_Mission.cpp:130-157",
               "AP463 libraries/AP_Mission/AP_Mission.h:45"],
          PX4: ["PX4 src/modules/navigator/mission_base.cpp:204-254"]}),
        ("MAV-25", "return_home", lambda ap: CMD_P(0, CMD["RTL"]), CMD["RTL"],
         {AP: ("AUTO", "RTL"), PX4: ("AUTO.MISSION", "AUTO.RTL")}, "return",
         {AP: "ArduCopter runs MAV_CMD_NAV_RETURN_TO_LAUNCH as a change to RTL mode (ACCEPTED, or FAILED).",
          PX4: "PX4 runs MAV_CMD_NAV_RETURN_TO_LAUNCH as a change to AUTO.RTL (ACCEPTED, or TEMPORARILY_REJECTED)."},
         {AP: ["AP463 ArduCopter/GCS_Mavlink.cpp:825-836"], PX4: ["PX4 src/modules/commander/Commander.cpp:1050-1062"]}),
        ("MAV-26", "land", lambda ap: CMD_P(0, CMD["LAND"]), CMD["LAND"],
         {AP: ("AUTO", "LAND"), PX4: ("AUTO.MISSION", "AUTO.LAND")}, "land",
         {AP: "ArduCopter runs MAV_CMD_NAV_LAND as a change to LAND mode, landing in place; params 5 and 6 are "
              "ignored.",
          PX4: "PX4 runs MAV_CMD_NAV_LAND as a forced change to AUTO.LAND, landing in place; the navigator does "
               "not read the command's position."},
         {AP: ["AP463 ArduCopter/GCS_Mavlink.cpp:825-836"], PX4: ["PX4 src/modules/commander/Commander.cpp:1093-1111"]}),
    ]
    for bid, op, first, command, modes, role, texts, extra in simple_ops:
        for ap in ap_or_px4:
            before, after = modes[ap]
            steps = [first(ap), ACK(40, command), HB(1000, ap, after, armed=True, flying=True),
                     Q(1000, {"mode": after, "role": role, "commanded": True})]
            cites = extra[ap] + C_CMD[ap] + (C_SETMODE[ap] if command == CMD["SET_MODE"] else [])
            add(_tr(bid, ap, op.replace("_", " ").capitalize(), texts[ap], cites, {"name": op, "args": {}},
                    steps, RES(40, "ok"), 2000, setup=_connected(ap, before, armed=True, flying=True)))

    # MAV-27 land escalation.
    land_cites = {AP: ["AP463 ArduCopter/GCS_Mavlink.cpp:825-836", "PLAN 2.4"] + C_SETMODE[AP],
                  PX4: ["PX4 src/modules/commander/Commander.cpp:1093-1111", "PLAN 2.4"] + C_SETMODE[PX4]}
    for ap in ap_or_px4:
        flying = _connected(ap, "AUTO" if ap == AP else "AUTO.MISSION", armed=True, flying=True)
        add(_tr("MAV-27", ap, "No ACK for NAV_LAND: land by mode",
                "Three unanswered MAV_CMD_NAV_LAND tries are followed at once by MAV_CMD_DO_SET_MODE to the "
                "autopilot's land mode (" + ("LAND, custom mode 9" if ap == AP else "AUTO.LAND") + "), which "
                "lands in place by the other command path.",
                land_cites[ap], {"name": "land", "args": {}},
                [CMD_P(0, CMD["LAND"]), CMD_P(1000, CMD["LAND"], 1), CMD_P(2000, CMD["LAND"], 2),
                 SET_MODE_P(3000, ap, "land"), ACK(3040, CMD["SET_MODE"])],
                RES(3040, "ok"), 5000, setup=flying, variant="silent"))
        refusal = FAILED if ap == AP else TEMP_REJECTED
        add(_tr("MAV-27", ap, "NAV_LAND refused: land by mode at once",
                "A refused MAV_CMD_NAV_LAND (" + ("FAILED" if ap == AP else "TEMPORARILY_REJECTED") + ") is not "
                "repeated; the phone moves to MAV_CMD_DO_SET_MODE land at once.",
                land_cites[ap], {"name": "land", "args": {}},
                [CMD_P(0, CMD["LAND"]), ACK(30, CMD["LAND"], refusal), SET_MODE_P(30, ap, "land"),
                 ACK(70, CMD["SET_MODE"])],
                RES(70, "ok"), 3000, setup=flying, variant="refused"))
    add(_tr("MAV-27", AP, "Land gets no answer at all",
            "Three NAV_LAND tries and three DO_SET_MODE tries without any ACK end in a timeout at 6 s; Land all "
            "shows 'no answer' for this drone.",
            land_cites[AP], {"name": "land", "args": {}},
            [CMD_P(0, CMD["LAND"]), CMD_P(1000, CMD["LAND"], 1), CMD_P(2000, CMD["LAND"], 2),
             SET_MODE_P(3000, AP, "land"), SET_MODE_P(4000, AP, "land", 1), SET_MODE_P(5000, AP, "land", 2)],
            RES(6000, "timeout"), 7500, setup=_connected(AP, "AUTO", armed=True, flying=True), variant="none"))

    # MAV-28 telemetry snapshot.
    for ap in ap_or_px4:
        mode = "AUTO" if ap == AP else "AUTO.MISSION"
        steps = [
            HB(100, ap, mode, armed=True, flying=True),
            V(200, "GLOBAL_POSITION_INT", time_boot_ms=184200, lat=406894450, lon=-1117306000, alt=1610050,
              relative_alt=10050, vx=300, vy=-12, vz=-5, hdg=9000),
            V(300, "SYS_STATUS", voltage_battery=15800, current_battery=1530, battery_remaining=76),
            V(400, "BATTERY_STATUS", id=0, battery_function=1, type=1, temperature=3150, voltages=[4200, 4190, 4180, 4170] + [65535] * 6,
              current_battery=1530, current_consumed=812, energy_consumed=-1, battery_remaining=74),
            V(500, "GPS_RAW_INT", fix_type=3, lat=406894450, lon=-1117306000, alt=1610050, eph=121, epv=180,
              satellites_visible=14),
            V(600, "EXTENDED_SYS_STATE", vtol_state=3 if ap == AP else 0, landed_state=2),
            V(700, "STATUSTEXT", severity=6, text="Mission: 3 WP" if ap == AP else "Takeoff detected"),
            Q(800, {"link_up": True, "control_link": True, "armed": True, "mode": mode, "role": "mission",
                    "lat_e7": 406894450, "lon_e7": -1117306000, "rel_alt_mm": 10050, "heading_cdeg": 9000,
                    "position_at_ms": 200, "battery_pct": 74, "battery_at_ms": 400, "voltage_mv": 15800,
                    "gps_fix": 3, "satellites": 14, "gps_at_ms": 500, "landed": "in_air", "landed_at_ms": 600}),
            V(1000, "BATTERY_STATUS", id=1, battery_function=1, type=1, temperature=32767, voltages=[65535] * 10,
              current_battery=-1, current_consumed=-1, energy_consumed=-1, battery_remaining=12),
            V(1100, "BATTERY_STATUS", id=0, battery_function=1, type=1, temperature=3150, voltages=[65535] * 10,
              current_battery=-1, current_consumed=-1, energy_consumed=-1, battery_remaining=-1),
            V(1200, "SYS_STATUS", voltage_battery=65535, current_battery=-1, battery_remaining=-1),
            V(1300, "GLOBAL_POSITION_INT", time_boot_ms=185300, lat=406894460, lon=-1117306000, alt=1610050,
              relative_alt=10040, hdg=65535),
            V(1400, "GPS_RAW_INT", fix_type=3, lat=406894460, lon=-1117306000, alt=1610040, eph=121, epv=180,
              satellites_visible=255),
            Q(1500, {"battery_pct": 74, "battery_at_ms": 400, "voltage_mv": 15800, "heading_cdeg": 9000,
                     "rel_alt_mm": 10040, "position_at_ms": 1300, "satellites": 14, "gps_at_ms": 1400}),
            Q(1500, [{"severity": 6, "text": "Mission: 3 WP" if ap == AP else "Takeoff detected"}], "statustext"),
        ]
        add(_tr("MAV-28", ap, "Telemetry snapshot",
                ("ArduCopter reports landed_state from land_complete and the mode (never UNDEFINED) and sends "
                 "EXTENDED_SYS_STATE only when requested." if ap == AP else
                 "PX4 reports landed_state from its land detector and the setpoint type.") +
                " Battery comes from BATTERY_STATUS instance 0, else SYS_STATUS; -1 (unknown) and other instances "
                "never overwrite a reading, so its time stays old and the stale-battery rule (R5) can act. "
                "UINT16_MAX heading or voltage and 255 satellites are unknown too.",
                (["AP463 ArduCopter/GCS_Mavlink.cpp:1560-1572"] if ap == AP else
                 ["PX4 src/modules/mavlink/streams/EXTENDED_SYS_STATE.hpp:102-118"]) +
                ["MAVLINK messages/common.html#SYS_STATUS", "MAVLINK messages/common.html#BATTERY_STATUS",
                 "MAVLINK messages/common.html#GLOBAL_POSITION_INT", "MAVLINK messages/common.html#GPS_RAW_INT",
                 "PLAN 2.2"],
                {"name": "observe", "args": {}}, steps, None, 2000,
                setup=_connected(ap, mode, armed=True, flying=True)))

    # MAV-29 mode report.
    add(_tr("MAV-29", AP, "Mode role, commanded flag and the AUTO return item",
            "ArduCopter reports the flight mode number as custom_mode. Its mission's NAV_RETURN_TO_LAUNCH item runs "
            "inside AUTO, so the mode stays AUTO to the end. A heartbeat that arrives while the hold command waits "
            "for its ACK still shows AUTO and is not judged. An RTL the phone did not command (here ArduCopter's "
            "own failsafe) is reported with commanded false, which the mission rules turn into EXTERNAL (R8).",
            ["AP463 ArduCopter/GCS_Mavlink.cpp:62-65", "AP463 ArduCopter/mode_auto.cpp:709-711", "PLAN 2.2"] + C_SETMODE[AP],
            {"name": "hold", "args": {}},
            [SET_MODE_P(0, AP, "hold"), HB(20, AP, "AUTO", armed=True, flying=True),
             Q(25, {"mode": "AUTO", "role": "mission", "commanded": True}), ACK(40, CMD["SET_MODE"]),
             HB(1000, AP, "GUIDED", armed=True, flying=True), Q(1000, {"mode": "GUIDED", "role": "hold", "commanded": True}),
             HB(2000, AP, "RTL", armed=True, flying=True), Q(2000, {"mode": "RTL", "role": "return", "commanded": False}),
             HB(3000, AP, "LOITER", armed=True, flying=True), Q(3000, {"mode": "LOITER", "role": "other", "commanded": False})],
            RES(40, "ok"), 3500, setup=_connected(AP, "AUTO", armed=True, flying=True)))
    add(_tr("MAV-29", PX4, "Mode role, commanded flag and the mission's return item",
            "PX4 runs a mission's NAV_RETURN_TO_LAUNCH item by issuing the RTL command to itself, so the mode "
            "changes to AUTO.RTL at the end of the mission. The phone treats that change as commanded when "
            "MISSION_CURRENT shows the last item (seq = total - 1); a mode the phone did not command, such as "
            "POSCTL from a pilot, is reported with commanded false.",
            ["PX4 src/modules/navigator/mission_block.cpp:533-555", "PX4 src/modules/navigator/mission.cpp:276",
             "PX4 src/modules/commander/Commander.cpp:1050-1062", "PX4 src/modules/mavlink/streams/HEARTBEAT.hpp:104",
             "PX4 src/modules/mavlink/mavlink_mission.cpp:284-292", "PLAN 2.2"],
            {"name": "observe", "args": {}},
            [V(0, "MISSION_CURRENT", seq=4, total=6, mission_state=3, mission_mode=1),
             HB(100, PX4, "AUTO.MISSION", armed=True, flying=True),
             Q(100, {"mode": "AUTO.MISSION", "role": "mission", "commanded": True}),
             V(1000, "MISSION_CURRENT", seq=5, total=6, mission_state=3, mission_mode=1),
             HB(1100, PX4, "AUTO.RTL", armed=True, flying=True),
             Q(1100, {"mode": "AUTO.RTL", "role": "return", "commanded": True}),
             HB(2100, PX4, "POSCTL", armed=True, flying=True),
             Q(2100, {"mode": "POSCTL", "role": "other", "commanded": False})],
            None, 2500, setup=dict(_connected(PX4, "AUTO.MISSION", armed=True, flying=True), commanded_role="mission")))

    # MAV-30 mission progress.
    for ap in ap_or_px4:
        offset = 1 if ap == AP else 0
        total = len(_wire_items(ap, MISSION))
        steps = []
        if ap == AP:
            steps += [V(0, "MISSION_CURRENT", seq=0, total=total), Q(0, {"mission_item": None})]
        for i, item in enumerate((0, 2, 5)):
            t = 1000 * (i + 1)
            steps += [V(t, "MISSION_CURRENT", seq=item + offset, total=total, mission_state=3),
                      Q(t, {"mission_item": item})]
        add(_tr("MAV-30", ap, "MISSION_CURRENT to plan item index",
                ("ArduCopter's sequence 0 is home, so its MISSION_CURRENT is one above the plan's index and 0 means "
                 "no plan item yet; MISSION_CURRENT is in its EXTENDED_STATUS stream, so the phone requests it."
                 if ap == AP else
                 "PX4's sequence 0 is the first plan item; its mission manager sends MISSION_CURRENT at 1 Hz and on "
                 "every change."),
                (["AP463 libraries/AP_Mission/AP_Mission.cpp:816-821", "AP463 ArduCopter/GCS_Mavlink.cpp:507"] if ap == AP else
                 ["PX4 src/modules/mavlink/mavlink_mission.cpp:284-292", "PX4 src/modules/mavlink/mavlink_mission.cpp:506-510"]) +
                ["MAVLINK messages/common.html#MISSION_CURRENT"],
                {"name": "observe", "args": {}}, steps, None, 3500,
                setup=_connected(ap, "AUTO" if ap == AP else "AUTO.MISSION", armed=True, flying=True)))

    # MAV-31 link liveness.
    for ap in ap_or_px4:
        mode = "AUTO" if ap == AP else "AUTO.MISSION"
        add(_tr("MAV-31", ap, "Link up and down",
                "The phone marks the link down after 3 s (link.loss_detect_s) without a packet from the connected "
                "vehicle; a packet from another system does not count. " +
                ("ArduCopter's own GCS failsafe acts after FS_GCS_TIMEOUT (default 5 s) without the phone's "
                 "heartbeat." if ap == AP else
                 "PX4's own data-link failsafe acts after COM_DL_LOSS_T (default 10 s) without the phone's "
                 "heartbeat.") +
                " Land all stays enabled because the control link stays true once any packet has arrived.",
                (["AP463 ArduCopter/Parameters.cpp:984-991", "AP463 ArduCopter/events.cpp:124-160"] if ap == AP else
                 ["PX4 src/modules/commander/commander_params.c:85-98", "PX4 src/modules/commander/Commander.cpp:2845-2858"]) +
                ["PLAN 2.2", "PLAN 2.4"],
                {"name": "observe", "args": {}},
                [HB(0, ap, mode, armed=True, flying=True), HB(1000, ap, mode, armed=True, flying=True),
                 V(2500, "HEARTBEAT", src={"sysid": 2, "compid": 1}, **_hb(ap, mode, armed=True, flying=True)),
                 Q(3999, {"link_up": True, "control_link": True}),
                 Q(4000, {"link_up": False, "control_link": True}),
                 HB(6000, ap, mode, armed=True, flying=True),
                 Q(6000, {"link_up": True, "control_link": True})],
                None, 6500, setup=_connected(ap, mode, armed=True, flying=True)))
    return out


def _validate_transcripts(transcripts: list) -> None:
    seen = set()
    for tr in transcripts:
        if tr["id"] in seen:
            raise RuntimeError("duplicate transcript id %s" % tr["id"])
        seen.add(tr["id"])
        for cite in tr["cites"]:
            if cite.split(" ", 1)[0] not in SOURCES:
                raise RuntimeError("%s cites unknown source %s" % (tr["id"], cite))
        last = -1
        for step in tr["steps"]:
            if step["t"] < last:
                raise RuntimeError("%s steps go back in time at %s" % (tr["id"], step))
            last = step["t"]
            if "msg" in step:
                if step["msg"] not in SUBSET:
                    raise RuntimeError("%s uses %s outside the subset" % (tr["id"], step["msg"]))
                src = step.get("src", {"sysid": GCS_SYSID, "compid": GCS_COMPID} if step["from"] == "phone"
                               else {"sysid": 1, "compid": 1})
                encode(step["msg"], step["fields"], sysid=src["sysid"], compid=src["compid"], seq=0)
    for bid in BEHAVIOUR_IDS:
        aps = {tr["autopilot"] for tr in transcripts if tr["behaviour"] == bid}
        if not {AP, PX4} <= aps:
            raise RuntimeError("%s lacks a transcript for %s" % (bid, sorted({AP, PX4} - aps)))


def _normalise(value):
    """Fields as stored in JSON: floats rounded to float32, NaN as a string."""
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    if isinstance(value, float):
        return "NaN" if math.isnan(value) else _f32(value)
    return value


def build_transcripts() -> dict:
    installed = _installed_pymavlink()
    if installed != PYMAVLINK_VERSION:
        raise RuntimeError("pymavlink %s is installed; the vectors pin %s" % (installed, PYMAVLINK_VERSION))
    transcripts = _transcripts()
    _validate_transcripts(transcripts)
    for tr in transcripts:
        for step in tr["steps"]:
            if "fields" in step:
                step["fields"] = _normalise(step["fields"])
        if "heartbeat" in tr["setup"]:
            tr["setup"]["heartbeat"] = _normalise(tr["setup"]["heartbeat"])
    return {
        "v": 1,
        "generator": GENERATOR,
        "pymavlink": PYMAVLINK_VERSION,
        "semantics": [
            "Each transcript runs one client operation ('op') against a scripted vehicle on a virtual clock in "
            "milliseconds; t = 0 is the moment the harness calls the op.",
            "Setup: with 'connected' true the harness first delivers 'setup.heartbeat' from the vehicle (sysid 1, "
            "compid 1) and lets the client finish connect before t = 0; the client sends nothing but heartbeats "
            "during setup. 'setup.commanded_role' (when present) is the role the phone last commanded before t = 0.",
            "A step with \"from\": \"vehicle\" is delivered at its t, from 'src' (default sysid 1, compid 1). A "
            "step with \"from\": \"phone\" must be sent by the client at its t, within 'timing.tolerance_ms', "
            "from sysid 255, compid 190. Steps with equal t happen in list order.",
            "Phone message matching: every listed field must be equal and every unlisted field must be zero (or "
            "empty); floats compare as float32 and \"NaN\" matches any NaN.",
            "Every message the client sends must match the next phone step, in order, except HEARTBEAT when "
            "'heartbeats' is \"ignore\". With 'heartbeats' \"match\", heartbeats are phone steps like any other. "
            "After the last step the client sends nothing (heartbeats aside) until 'end_ms'.",
            "A \"query\" step reads the client at its t: \"snapshot\" must contain every key of 'expect' with that "
            "value (null means unknown); \"statustext\" must equal the list of STATUSTEXT messages received so far.",
            "'result' (null for observe-only transcripts) is how the op must resolve and when: outcome \"ok\", "
            "\"refused\" or \"timeout\" with the listed data. These map to CommandResult ok, refused and timeout "
            "of plan section 2.2.",
            "Retries: a command, a parameter name and MISSION_COUNT are tried up to 'timing.tries' times, "
            "'timing.retry_ms' apart; the op times out 'retry_ms' after the last try. During an upload the op "
            "times out 'mission_idle_ms' after the last request. One COMMAND_LONG is outstanding at a time.",
            "Results not in 'refused' or 'timeout': ACK result 0 (ACCEPTED) is ok. Any other ACK result is refused, "
            "never retried. No v1 command expects IN_PROGRESS (5); a client treats it as not final and keeps the "
            "same deadline.",
            "Mode roles: 'modes' maps each autopilot's custom_mode to a name and a role (mission, takeoff, hold, "
            "return, land, other); an unknown custom_mode is named MODE_<n> with role other. 'commanded' is true "
            "while the role matches the role the phone last commanded and got ACCEPTED for (start and resume: "
            "mission, which also allows takeoff, and return once MISSION_CURRENT shows seq = total - 1; hold: "
            "hold; return_home: return; land: land). At connect the current role counts as commanded. Heartbeats "
            "that arrive while a mode command waits for its ACK do not change 'commanded'.",
            "Snapshot keys: link_up, control_link, sysid, compid, autopilot, armed (base_mode bit 128), mode, role, "
            "commanded, lat_e7, lon_e7, rel_alt_mm, heading_cdeg, position_at_ms (GLOBAL_POSITION_INT), "
            "battery_pct, battery_at_ms, voltage_mv, gps_fix, satellites, gps_at_ms, landed (on_ground, in_air, "
            "takeoff, landing or null), landed_at_ms, mission_item, home. A reading of -1, UINT16_MAX or 255 "
            "(where the message defines it as unknown) leaves the previous value and its time unchanged.",
            "Bits of base_mode other than 128 (armed) and 1 (custom mode) and system_status vary with firmware "
            "state; the vehicles here use plausible values and clients must not depend on them.",
        ],
        "sources": SOURCES,
        "citation_format": "'<source key> <path>:<line or first-last>' resolves to sources[key].url + path + "
                           "'#L<line>'; for MAVLINK and PLAN the rest is a page or a section.",
        "gcs": {"sysid": GCS_SYSID, "compid": GCS_COMPID},
        "timing": TIMING,
        "streams": [{"msgid": m, "interval_us": i} for m, i in STREAMS],
        "multirotor_types": [{"type": k, "name": v} for k, v in MULTIROTOR_TYPES.items()],
        "modes": {ap: [{"custom_mode": cm, "name": n, "role": r} for cm, n, r in rows] for ap, rows in MODES.items()},
        "set_mode": {ap: {role: list(p) for role, p in roles.items()} for ap, roles in SET_MODE.items()},
        "mission_items": {
            "takeoff": "MAV_CMD_NAV_TAKEOFF (22), frame 3, param4 NaN, x/y the takeoff point, z alt_cm / 100",
            "speed": "MAV_CMD_DO_CHANGE_SPEED (178), frame 2, param1 1 (ground speed), param2 cm_s / 100, param3 -1",
            "waypoint": "MAV_CMD_NAV_WAYPOINT (16), frame 3, param1-3 0, param4 NaN, z alt_cm / 100",
            "return": "MAV_CMD_NAV_RETURN_TO_LAUNCH (20), frame 2, all zero",
            "home": "ArduPilot only, sequence 0: MAV_CMD_NAV_WAYPOINT (16), frame 0, x/y home, z 0",
            "all": "current 0, autocontinue 1, mission_type 0",
        },
        "behaviours": [{"id": b, "name": n, "plan": p, "text": t} for b, n, p, t in BEHAVIOURS],
        "transcripts": transcripts,
    }


OUTPUTS = {
    "mavlink_frames.json": build_frames,
    "mavlink_transcripts.json": build_transcripts,
}


def render(obj: dict) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=True, allow_nan=False) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail if a committed file differs from a fresh build")
    mode.add_argument("--write", action="store_true", help="rewrite the files")
    args = parser.parse_args(argv)
    status = 0
    for name, build in OUTPUTS.items():
        path = OUT_DIR / name
        text = render(build())
        if args.write:
            path.write_text(text, encoding="utf-8")
            print("wrote %s" % path.relative_to(ROOT))
        elif not path.exists() or path.read_text(encoding="utf-8") != text:
            print("%s differs from a fresh build" % path.relative_to(ROOT), file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
