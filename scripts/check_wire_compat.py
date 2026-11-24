"""Prove that today's fleet protocol still talks to a peer running an older commit (CMP-1).

Phase A2 adds wire shapes (``describe_unit``, the return commands, the
``rth`` telemetry object, ``unit_rev`` in the edge ``hello``, the
``assign_lanes`` return keys and the ``straight_line_home`` nack reason) and
must not break a peer that runs the code from before A2. This script checks
that mechanically:

1. It extracts ``flightrisk/fleet_protocol`` at the base commit with
   ``git show`` into a temporary directory and imports it under another
   module name, so the base validators run beside today's code.
2. It builds a corpus of messages from today's builders (``corpus``): each
   builder with the argument combinations listed there, the legacy
   messages and every A2 shape and key. It is not every possible message.
   It shapes each one for a protocol v1 and a
   protocol v2 peer with today's ``shape_for_peer``, exactly as a sender
   does. A message that today's code refuses to send on a link is skipped
   for that link.
3. Each message an older peer could receive is encoded with today's
   ``encode`` and must be accepted by the base ``decode`` for its direction
   and link version (and by the strict form an edge with ``require_fence``
   uses, wherever today's edge accepts it in that form).
4. A message that the A2 gates keep away from an older peer is not required
   to pass. The gates are today's own helpers: ``may_describe`` (a ground
   asks only an edge whose ``hello`` carries ``unit_rev``), ``may_send``
   (a ground sends a return command or an ``assign_lanes`` return key only
   to an edge whose ``describe_unit`` reply lists it in ``accepts``),
   replies to those commands, and ``nack_reason_for_peer`` (an edge sends
   ``straight_line_home`` only to a ground that has sent it
   ``describe_unit``). Each gated message is checked against the base
   validators too, which shows the gate is needed.

Usage::

    venv/bin/python scripts/check_wire_compat.py --base dab72db

It prints ``compatible: N messages, gated: M refused by base, K accepted by
base but stripped of meaning (now refused by the send path)`` and exits 0,
or lists every incompatible message and exits 1. A gated message that the
base accepts (an older edge would drop the new key without refusing it) is
listed as a note, and today's ``shape_for_peer`` must refuse it for a peer
that has not proved it accepts the key; if it does not, the message is
incompatible. The temporary directory is removed before the script exits.

``tests/test_fleet_protocol_compat.py`` runs the same check as a test and
adds the reverse direction (the base builders' messages under today's
validators).
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flightrisk import fleet_protocol as current  # noqa: E402

PACKAGE = "flightrisk.fleet_protocol"
PACKAGE_DIR = "flightrisk/fleet_protocol"
PEER_PROTOS = (1, 2)
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "mobile_contract"
FIXTURE_FILES = ("edge_ground_to_edge.json", "edge_edge_to_ground.json")
REV = "0123456789ab"


@dataclass(frozen=True)
class Sample:
    """One message a builder produced, with the direction it travels in."""

    name: str
    direction: str
    msg: Mapping[str, Any]


@dataclass
class Report:
    """The outcome of a forward check."""

    compatible: list[str] = field(default_factory=list)
    gated_refused: list[str] = field(default_factory=list)
    gated_accepted: list[str] = field(default_factory=list)
    incompatible: list[str] = field(default_factory=list)
    unsendable: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.incompatible

    def summary(self) -> str:
        return (
            f"compatible: {len(self.compatible)} messages, gated: {len(self.gated_refused)} refused by base, "
            f"{len(self.gated_accepted)} accepted by base but stripped of meaning (now refused by the send path)"
        )


# --- the base package ---------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True
    ).stdout


@contextmanager
def base_package(base: str) -> Iterator[ModuleType]:
    """Import ``flightrisk.fleet_protocol`` as it was at commit *base*, under another module name.

    The files are read with ``git show`` into a temporary directory, and
    every ``flightrisk.fleet_protocol`` import in them is renamed to the new
    module name, so the base code never sees today's package. Every
    submodule is imported before the caller gets the package. On exit the
    modules are dropped from ``sys.modules`` and the directory is removed.
    """
    sha = _git("rev-parse", "--verify", f"{base}^{{commit}}").strip()
    name = f"_wire_compat_base_{sha[:12]}"
    paths = [p for p in _git("ls-tree", "--name-only", sha, PACKAGE_DIR + "/").split() if p.endswith(".py")]
    if not paths:
        raise SystemExit(f"commit {sha[:12]} has no {PACKAGE_DIR} package")
    saved_bytecode = sys.dont_write_bytecode
    with tempfile.TemporaryDirectory(prefix="wire-compat-") as tmp:
        package_dir = Path(tmp) / name
        package_dir.mkdir()
        for path in paths:
            source = _git("show", f"{sha}:{path}")
            (package_dir / Path(path).name).write_text(source.replace(PACKAGE, name), encoding="utf-8")
        sys.path.insert(0, tmp)
        sys.dont_write_bytecode = True
        try:
            module = importlib.import_module(name)
            for path in paths:
                if Path(path).stem != "__init__":
                    importlib.import_module(f"{name}.{Path(path).stem}")
            module.__wire_compat_sha__ = sha
            yield module
        finally:
            sys.dont_write_bytecode = saved_bytecode
            sys.path.remove(tmp)
            for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
                del sys.modules[key]


# --- the corpus ---------------------------------------------------------------


def common_corpus(fp: ModuleType) -> list[Sample]:
    """Every message the builders of *fp* produce with the arguments that existed before A2.

    Only builder arguments that both the base and today's package accept
    are used, so the same function builds the reverse-direction corpus from
    the base builders.
    """
    g2e, e2g, peer = fp.DIRECTION_GROUND_TO_EDGE, fp.DIRECTION_EDGE_TO_GROUND, fp.DIRECTION_PEER
    fence = fp.Fence(7, "ground-a")
    lanes = [{"direction": "forward", "distance_cm": 200}, {"direction": "right", "distance_cm": 100}]
    zone = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}
    out: list[Sample] = []

    def add(name: str, direction: str, build: Callable[[], Mapping[str, Any]]) -> None:
        out.append(Sample(name, direction, build()))

    add("ping", g2e, lambda: fp.make_ping(1000.0))
    add("clock", g2e, lambda: fp.make_clock(0.25))
    add("clock_fenced", g2e, lambda: fp.make_clock(0.25, fence=fence))
    add("heartbeat", g2e, lambda: fp.make_heartbeat(12))
    add("heartbeat_fenced", g2e, lambda: fp.make_heartbeat(12, fence=fence))
    add("assign_lanes_v1", g2e, lambda: fp.make_assign_lanes("drone-1", 100, lanes, zone))
    for action in sorted(fp.ON_COMPLETE_ACTIONS):
        timeout = {"hover_timeout_s": 30.0} if action == "hover" else {}
        add(f"assign_lanes_v2_{action}", g2e, lambda a=action, t=timeout: fp.make_assign_lanes(
            "drone-1", 100, lanes, zone, fence=fence, cmd_id="c-al", plan_id="p-1", plan_seq=3,
            on_complete=a, **t))
    add("start_at_v1", g2e, lambda: fp.make_start_at(1234.5))
    add("start_at_v2", g2e, lambda: fp.make_start_at(1234.5, fence=fence, plan_id="p-1", cmd_id="c-st"))
    add("takeoff_v1", g2e, lambda: fp.make_takeoff())
    add("takeoff_v2", g2e, lambda: fp.make_takeoff(fence=fence, cmd_id="c-to"))
    add("land", g2e, lambda: fp.make_land(cmd_id="c-land"))
    add("land_bare", g2e, lambda: fp.make_land())
    add("emergency_land_all", g2e, fp.make_emergency_land_all)
    for role in sorted(fp.GROUND_ROLES):
        add(f"ground_hello_{role}", g2e, lambda r=role: fp.make_ground_hello("ground-a", r, 7))
    for reason in sorted(fp.HOLD_REASONS):
        add(f"hold_{reason}", g2e, lambda r=reason: fp.make_hold(fence, cmd_id="c-h", reason=r))
    add("hold_bare", g2e, lambda: fp.make_hold(fence, cmd_id="c-h"))
    add("resume", g2e, lambda: fp.make_resume(fence, plan_id="p-1", cmd_id="c-r"))
    add("nudge", g2e, lambda: fp.make_nudge(fence, "left", 50, cmd_id="c-n"))
    add("handover", g2e, lambda: fp.make_handover(fence, "ground-b", cmd_id="c-ho"))
    add("set_detection_profile", g2e, lambda: fp.make_set_detection_profile("fast", drone_id="drone-1", cmd_id="c-p"))
    add("set_ceiling", g2e, lambda: fp.make_set_ceiling(200, 60, 100, drone_id="drone-1", cmd_id="c-ce"))
    add("set_ceiling_fenced", g2e, lambda: fp.make_set_ceiling(
        200, 60, 100, drone_id="drone-1", cmd_id="c-ce", fence=(7, "ground-a")))
    add("manual_hold", g2e, lambda: fp.make_manual_hold(drone_id="drone-1", cmd_id="c-mh", fence=fence))
    add("manual_takeoff", g2e, lambda: fp.make_manual_takeoff(drone_id="drone-1", cmd_id="c-mt", fence=fence))
    add("resume_search", g2e, lambda: fp.make_resume_search(drone_id="drone-1", cmd_id="c-rs", fence=fence))
    add("manual_land", g2e, lambda: fp.make_manual_land(cmd_id="c-ml"))
    add("manual_move", g2e, lambda: fp.make_manual_move(
        "forward", distance_cm=50, drone_id="drone-1", cmd_id="c-mm", fence=fence))
    add("manual_move_rotate", g2e, lambda: fp.make_manual_move(
        "cw", degrees=30, drone_id="drone-1", cmd_id="c-mm", fence=fence))
    # The legacy messages pass by type only; these are the shapes the app sends.
    add("set_target", g2e, lambda: {"type": "set_target", "drone_id": "drone-1", "label": "person"})
    add("stream_video", g2e, lambda: {"type": "stream_video", "drone_id": "drone-1", "enabled": True})

    add("pong", e2g, lambda: fp.make_pong(1000.0, 1000.25, 1000.26))
    tel = ("drone-1", 82, 100, True, 10.0, -5.0, 90.0, 1, 4, 0.1, 1000.0)
    add("telemetry_v1", e2g, lambda: fp.make_telemetry(*tel))
    for state in sorted(fp.EDGE_STATES):
        add(f"telemetry_v2_{state}", e2g, lambda s=state: fp.make_telemetry(
            *tel, v2=fp.TelemetryV2(epoch=7, plan_id="p-1", state=s, plan_valid=True)))
    add("ack_start_at_v1", e2g, lambda: fp.make_ack_start_at("drone-1", 1234.6))
    add("ack_start_at_v2", e2g, lambda: fp.make_ack_start_at("drone-1", 1234.6, cmd_id="c-st"))
    add("ack_assign_lanes_v1", e2g, lambda: fp.make_ack_assign_lanes("drone-1", 90, 8))
    add("ack_assign_lanes_v2", e2g, lambda: fp.make_ack_assign_lanes("drone-1", 90, 8, cmd_id="c-al"))
    for reason in sorted(fp.NACK_REASONS):
        geo = {"geofence": {"geofence_cm": 450.0}} if reason == "geofence" else {}
        add(f"nack_assign_lanes_{reason}", e2g, lambda r=reason, g=geo: fp.make_nack_assign_lanes("drone-1", r, 8, **g))
    add("ack_hold", e2g, lambda: fp.make_ack("hold", "drone-1", "c-h", epoch_seen=7))
    add("ack_land", e2g, lambda: fp.make_ack("land", "drone-1", None))
    add("ack_takeoff", e2g, lambda: fp.make_ack("takeoff", "drone-1", "c-to", epoch_seen=7))
    add("ack_resume", e2g, lambda: fp.make_ack("resume", "drone-1", "c-r", epoch_seen=7))
    add("ack_nudge", e2g, lambda: fp.make_ack("nudge", "drone-1", "c-n", epoch_seen=7))
    add("ack_handover", e2g, lambda: fp.make_ack("handover", "drone-1", "c-ho", epoch_seen=7))
    add("ack_emergency_land_all", e2g, lambda: fp.make_ack("emergency_land_all", "drone-1", None))
    add("detections", e2g, lambda: {"type": "detections", "drone_id": "drone-1", "detections": []})
    for reason in sorted(fp.NACK_REASONS_V2):
        add(f"nack_takeoff_{reason}", e2g, lambda r=reason: fp.make_nack(
            "takeoff", "drone-1", r, "c-to", epoch_seen=7, detail="refused"))
    add("nack_assign_lanes_v2", e2g, lambda: fp.make_nack("assign_lanes", "drone-1", "battery", "c-al", lanes_total=8))
    add("edge_hello", e2g, lambda: fp.make_edge_hello(
        "drone-1", "edge-1", 7, plan_id="p-1", lane_index=3, is_flying=False, battery=71))
    add("edge_hello_v1", e2g, lambda: fp.make_edge_hello(
        "drone-1", "edge-1", 0, plan_id=None, lane_index=None, is_flying=False, battery=None, proto=1))
    add("profile_ack", e2g, lambda: fp.make_profile_ack("drone-1", "c-p", "fast"))
    add("profile_ack_fallback", e2g, lambda: fp.make_profile_ack(
        "drone-1", "c-p", "fast", requested="accurate", fallback=True, fallback_reason="weights_missing"))
    add("profile_nack", e2g, lambda: fp.make_profile_nack("drone-1", "c-p", "in_flight", profile="fast"))
    active = {"ceiling_cm": 200, "ceiling_margin_cm": 60, "alt_slot_cm": 100}
    add("ceiling_ack", e2g, lambda: fp.make_ceiling_ack("drone-1", "c-ce", active))
    add("ceiling_nack", e2g, lambda: fp.make_ceiling_nack("drone-1", "c-ce", "invalid", active=active, detail="bad"))
    add("manual_ack", e2g, lambda: fp.make_manual_ack("manual_hold", "drone-1", "c-mh"))
    add("manual_nack", e2g, lambda: fp.make_manual_nack("manual_hold", "drone-1", "c-mh", "invalid"))

    add("node_state", peer, lambda: fp.make_node_state("ground-a", "leader", 7))
    add("handover_offer", peer, lambda: fp.make_handover_offer(8, fp.make_node_state("ground-a", "leader", 7)))
    add("handover_accept", peer, lambda: fp.make_handover_accept(8))
    add("handover_reject", peer, lambda: fp.make_handover_reject("busy", epoch_next=8))
    add("peer_heartbeat", peer, lambda: fp.make_peer_heartbeat(3, 7, node_id="ground-a"))
    return out


def _describe_unit_ack(fp: ModuleType, accepts: list[str]) -> Mapping[str, Any]:
    from flightrisk.hardware.catalogue import BUILTIN_MODELS
    from flightrisk.params.report import build_report
    from flightrisk.params.resolver import resolve_unit

    report = build_report(BUILTIN_MODELS["tello"], resolve_unit("tello", "drone-1", {}))
    return fp.make_describe_unit_ack("drone-1", "c-du", **report.wire(), ap_link_loss="unknown", accepts=accepts)


def a2_corpus(fp: ModuleType = current) -> list[Sample]:
    """The messages only today's builders produce: every A2 wire shape and key."""
    g2e, e2g = fp.DIRECTION_GROUND_TO_EDGE, fp.DIRECTION_EDGE_TO_GROUND
    fence = fp.Fence(7, "ground-a")
    pair = (7, "ground-a")
    lanes = [{"direction": "forward", "distance_cm": 200}]
    zone = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}
    square = [{"x_cm": 0, "y_cm": 0}, {"x_cm": 300, "y_cm": 0}, {"x_cm": 300, "y_cm": 300}]
    area = {"id": "cue-1", "kind": "person_cue", "points": square}
    v2 = fp.TelemetryV2(epoch=7, plan_id="p-1", state="flying_lanes", plan_valid=True)
    tel = ("drone-1", 82, 100, True, 10.0, -5.0, 90.0, 1, 4, 0.1, 1000.0)
    plan = {"fence": fence, "cmd_id": "c-al", "plan_id": "p-1", "plan_seq": 3}
    out: list[Sample] = []

    def add(name: str, direction: str, build: Callable[[], Mapping[str, Any]]) -> None:
        out.append(Sample(name, direction, build()))

    # Not gated: an older peer receives these and must accept them.
    add("edge_hello_unit_rev", e2g, lambda: fp.make_edge_hello(
        "drone-1", "edge-1", 7, plan_id="p-1", lane_index=3, is_flying=True, battery=71, unit_rev=REV))
    full_rth = fp.make_rth_object(
        "rth_returning", reason="critical_battery", home=(0, 0), distance_cm=1200, uncertainty_cm=50,
        reserve_pct=30, threshold_pct=25, countdown_s=0, cannot_recall=False, landing_area_id="pad-1",
        landing_place=0, ap_link_loss="return")
    add("telemetry_rth_full", e2g, lambda: fp.make_telemetry(*tel, v2=v2, rth=full_rth))
    add("telemetry_rth_state_only", e2g, lambda: fp.make_telemetry(*tel, v2=v2, rth=fp.make_rth_object("flying")))
    for action in sorted(fp.AP_LINK_LOSS_ACTIONS):
        add(f"telemetry_rth_ap_link_loss_{action}", e2g, lambda a=action: fp.make_telemetry(
            *tel, v2=v2, rth=fp.make_rth_object("flying", ap_link_loss=a)))
    for state in sorted(fp.RTH_STATES):
        add(f"telemetry_rth_{state}", e2g, lambda s=state: fp.make_telemetry(*tel, v2=v2, rth=fp.make_rth_object(s)))
    add("nack_straight_line_home_for_old_ground", e2g, lambda: fp.make_nack(
        "takeoff", "drone-1", fp.nack_reason_for_peer("straight_line_home", None), "c-to", detail="no route home"))

    # Gated: a ground sends these only after the edge's describe_unit reply lists them.
    add("describe_unit", g2e, lambda: fp.make_describe_unit("drone-1", cmd_id="c-du"))
    add("return_home", g2e, lambda: fp.make_return_home("drone-1", cmd_id="c-rh"))
    add("return_home_wait", g2e, lambda: fp.make_return_home(
        "drone-1", cmd_id="c-rh", fence=pair, wait=True, max_wait_s=40))
    add("return_home_release", g2e, lambda: fp.make_return_home("drone-1", cmd_id="c-rh", fence=pair, wait=False))
    add("set_no_entry", g2e, lambda: fp.make_set_no_entry("drone-1", [area], fence=pair, cmd_id="c-ne"))
    add("set_no_entry_empty", g2e, lambda: fp.make_set_no_entry("drone-1", [], fence=pair, cmd_id="c-ne"))
    add("set_speed", g2e, lambda: fp.make_set_speed("drone-1", 150, fence=pair, cmd_id="c-sp"))
    add("escort_setpoint", g2e, lambda: fp.make_escort_setpoint(
        "drone-1", 1200, -400, 1500, 90, 2, fence=pair, cmd_id="c-es"))
    add("assign_lanes_on_complete_return_home", g2e, lambda: fp.make_assign_lanes(
        "drone-1", 100, lanes, zone, **plan, on_complete="return_home", hover_timeout_s=5))
    add("assign_lanes_corridor", g2e, lambda: fp.make_assign_lanes("drone-1", 100, lanes, zone, **plan, corridor=square))
    add("assign_lanes_landing", g2e, lambda: fp.make_assign_lanes(
        "drone-1", 100, lanes, zone, **plan, landing={"area_id": "pad-1", "place": 0, "interval_s": 5}))
    add("assign_lanes_no_entry", g2e, lambda: fp.make_assign_lanes("drone-1", 100, lanes, zone, **plan, no_entry=[area]))
    add("assign_lanes_speed", g2e, lambda: fp.make_assign_lanes("drone-1", 100, lanes, zone, **plan, speed_cm_s=150))

    # Gated replies: an edge sends these only in answer to a gated query or command.
    add("describe_unit_ack", e2g, lambda: _describe_unit_ack(fp, ["return_home"]))
    add("describe_unit_ack_all_accepts", e2g, lambda: _describe_unit_ack(fp, sorted(fp.ACCEPT_NAMES - {"escort_setpoint"})))
    add("describe_unit_nack", e2g, lambda: fp.make_describe_unit_nack("drone-1", "c-du", "busy", detail="too fast"))
    for mtype in sorted(fp.RETURN_COMMAND_TYPES):
        command = {"type": mtype, "drone_id": "drone-1", "cmd_id": "c-rc"}
        add(f"ack_{mtype}", e2g, lambda c=command: fp.make_return_ack(c, epoch_seen=7))
        add(f"nack_{mtype}", e2g, lambda c=command: fp.make_return_nack(c, "landing", detail="landing now"))
    add("nack_return_home_straight_line_home", e2g, lambda: fp.make_return_nack(
        {"type": "return_home", "drone_id": "drone-1", "cmd_id": "c-rh"}, "straight_line_home"))
    add("nack_straight_line_home", e2g, lambda: fp.make_nack(
        "takeoff", "drone-1", "straight_line_home", "c-to", detail="no route home"))
    for for_type, extra in (("hold", {}), ("assign_lanes", {"lanes_total": 8})):
        for reason in ("straight_line_home", fp.nack_reason_for_peer("straight_line_home", None)):
            add(f"nack_{for_type}_{reason}", e2g, lambda f=for_type, r=reason, x=extra: fp.make_nack(
                f, "drone-1", r, "c-x", detail="no route home", **x))
    for reason in ("straight_line_home", fp.nack_reason_for_peer("straight_line_home", None)):
        add(f"nack_manual_takeoff_{reason}", e2g, lambda r=reason: fp.make_manual_nack(
            "manual_takeoff", "drone-1", "c-mt", r))
    return out


def corpus(fp: ModuleType = current) -> list[Sample]:
    """Every message today's builders can produce: the pre-A2 shapes and the A2 shapes."""
    return common_corpus(fp) + a2_corpus(fp)


def fixture_samples() -> list[Sample]:
    """The edge-link payloads of the mobile contract fixtures."""
    out: list[Sample] = []
    for name in FIXTURE_FILES:
        cases = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))["cases"]
        for case, body in sorted(cases.items()):
            out.append(Sample(f"{name}:{case}", body["direction"], body["payload"]))
    return out


# --- the gates ----------------------------------------------------------------


#: The part of a ``describe_unit`` ack that ``may_send`` reads, listing every
#: name, as a sender holds it once the edge has proved it accepts the new keys.
PROOF: Mapping[str, Any] = {"accepts": sorted(current.ACCEPT_NAMES)}


def _older_edge_hello() -> Mapping[str, Any]:
    """An edge ``hello`` as an edge from before A2 sends it: no ``unit_rev``."""
    return current.make_edge_hello("drone-1", "edge-1", 7, plan_id=None, lane_index=None, is_flying=False, battery=80)


def gate_of(msg: Mapping[str, Any]) -> str | None:
    """Why today's gates keep *msg* away from an older peer, or None when they do not.

    Uses today's helpers only. ``describe_unit`` goes only to an edge whose
    ``hello`` passes ``may_describe``; anything ``may_send`` refuses without
    a description needs the edge's ``accepts``; a reply to either exists
    only after such a query or command; a nack reason that
    ``nack_reason_for_peer`` rewrites for a ground with no query goes only
    to a ground that sent ``describe_unit``.
    """
    mtype = msg.get("type")
    if mtype == current.DESCRIBE_UNIT:
        return None if current.may_describe(_older_edge_hello()) else "may_describe: older edge hello has no unit_rev"
    if not current.may_send(None, msg):
        return f"may_send: needs accepts {sorted(current.required_accepts(msg))}"
    if current.is_describe_unit_reply(msg):
        return "reply to describe_unit, which an older ground never sends"
    if current.is_return_reply(msg):
        return f"reply to {msg.get('for')!r}, which needs accepts"
    if mtype == current.NACK and isinstance(msg.get("reason"), str):
        if current.nack_reason_for_peer(msg["reason"], None) != msg["reason"]:
            return "nack_reason_for_peer: reason only for a ground that sent describe_unit"
    return None


# --- the checks ---------------------------------------------------------------


def _accepts(fp: ModuleType, text: str, direction: str, proto: int, *, require_fence: bool = False) -> str | None:
    """None when *fp* decodes *text*, else the refusal."""
    kwargs = {"require_fence": True} if require_fence else {}
    try:
        fp.decode(text, direction=direction, proto=proto, **kwargs)
    except fp.ProtocolError as exc:
        return str(exc)
    return None


def _links(sample: Sample, report: Report) -> Iterator[tuple[int, str]]:
    """Each ``(peer_proto, wire text)`` today's code sends *sample* as.

    A gated ground-to-edge message (``describe_unit``, a return command) is
    refused by today's strict ``encode`` too, because today's edge does not
    handle it yet (C1). It is serialised after ``shape_for_peer`` and
    ``validate_message`` instead, so the base validators still see it.
    """
    sent = False
    for proto in PEER_PROTOS:
        try:
            # A sender that has the edge's proof: the gated keys still reach
            # the base validators, so the check shows whether a gate is needed.
            shaped = current.shape_for_peer(sample.msg, proto, peer_description=PROOF)
            try:
                text = current.encode(shaped, direction=sample.direction, proto=proto)
            except current.ProtocolError:
                if gate_of(shaped) is None:
                    raise
                current.validate_message(shaped, proto=proto)
                text = json.dumps(shaped, separators=(",", ":"), allow_nan=False)
        except current.ProtocolError:
            continue
        sent = True
        yield proto, text
    if not sent:
        report.unsendable.append(sample.name)


def _send_path_refuses(msg: Mapping[str, Any], proto: int) -> bool:
    """True when today's ``shape_for_peer`` refuses *msg* for a peer with no ``accepts`` proof."""
    try:
        current.shape_for_peer(msg, proto)
    except current.ProtocolError:
        return True
    return False


def check_forward(base: ModuleType, samples: list[Sample] | None = None) -> Report:
    """Today's messages, as sent to each peer version, under the base validators."""
    report = Report()
    for sample in corpus() if samples is None else samples:
        for proto, text in _links(sample, report):
            label = f"{sample.name} (v{proto})"
            gate = gate_of(json.loads(text))
            refusal = _accepts(base, text, sample.direction, proto)
            if gate is not None:
                if refusal:
                    report.gated_refused.append(f"{label}: {gate}")
                elif _send_path_refuses(sample.msg, proto):
                    report.gated_accepted.append(f"{label}: {gate}")
                else:
                    report.incompatible.append(
                        f"{label}: accepted by base without its meaning, and the send path does not refuse it"
                    )
                continue
            if refusal is None and sample.direction == current.DIRECTION_GROUND_TO_EDGE and proto >= 2:
                # An edge that requires the v2 form on every link must still
                # accept what today's edge accepts in that form.
                if _accepts(current, text, sample.direction, proto, require_fence=True) is None:
                    refusal = _accepts(base, text, sample.direction, proto, require_fence=True)
            if refusal is None:
                report.compatible.append(label)
            else:
                report.incompatible.append(f"{label}: {refusal} -- {text}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base", required=True, help="the pre-A2 commit whose fleet_protocol older peers run")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every message checked")
    args = parser.parse_args(argv)
    with base_package(args.base) as base:
        report = check_forward(base)
    if args.verbose:
        for label in report.compatible:
            print(f"ok      {label}")
        for label in report.gated_refused:
            print(f"gated   {label}")
    for label in report.gated_accepted:
        print(f"note: gated, accepted by base, refused by the send path without accepts: {label}")
    for name in report.unsendable:
        print(f"INCOMPATIBLE {name}: today's code sends it on no link")
    for label in report.incompatible:
        print(f"INCOMPATIBLE {label}")
    print(report.summary())
    return 0 if report.ok and not report.unsendable else 1


if __name__ == "__main__":
    sys.exit(main())
