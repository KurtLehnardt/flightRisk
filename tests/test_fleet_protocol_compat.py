"""Phase A2 keeps older peers working (CMP-1): the cross-version wire proof.

``scripts/check_wire_compat.py`` loads ``flightrisk/fleet_protocol`` as it
was at ``BASE`` (the A1 merge, before any A2 change) and checks today's
messages against it. These tests run the same check and add:

* the reverse direction: the base builders' messages, and the mobile
  contract fixtures, are accepted by today's validators at protocol v1 and
  v2, so an older peer can still talk to a new one;
* byte identity: today's builders, called with the arguments that existed
  before A2, produce exactly the base builders' JSON;
* the ``shape_for_peer`` rules: every new key is stripped, or the message
  refused, for a protocol v1 peer;
* the real edge's worst-case telemetry with ``rth`` under the base
  validators.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.fleet_protocol.builders import TelemetryV2

ROOT = Path(__file__).resolve().parent.parent
BASE = "dab72db"
#: Set to 1 to skip the cross-version proof when the base commit is missing.
SKIP_ENV = "FLIGHTRISK_SKIP_WIRE_COMPAT"

_spec = importlib.util.spec_from_file_location("check_wire_compat", ROOT / "scripts" / "check_wire_compat.py")
compat = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_wire_compat", compat)
_spec.loader.exec_module(compat)


def _have_base() -> bool:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{BASE}^{{commit}}"], capture_output=True
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def base():
    if not _have_base():
        message = (
            f"the base commit {BASE} is not in this clone (a shallow checkout), so the compatibility proof "
            f"cannot run; fetch the history with 'git fetch --unshallow' (or 'git fetch origin {BASE}')"
        )
        if os.environ.get(SKIP_ENV) == "1":
            pytest.skip(f"{message}; skipped because {SKIP_ENV}=1")
        pytest.fail(f"{message}, or set {SKIP_ENV}=1 to skip it on purpose")
    with compat.base_package(BASE) as module:
        yield module


@pytest.fixture(scope="module")
def forward(base):
    return compat.check_forward(base)


# --- forward: today's messages reach an older peer --------------------------


def test_every_message_an_older_peer_can_receive_is_accepted_by_the_base(forward):
    assert forward.incompatible == []
    assert forward.unsendable == []
    assert len(forward.compatible) > 100
    assert forward.summary() == (
        f"compatible: {len(forward.compatible)} messages, gated: {len(forward.gated_refused)} refused by base, "
        f"{len(forward.gated_accepted)} accepted by base but stripped of meaning (now refused by the send path)"
    )


@pytest.mark.parametrize("name", ["edge_hello_unit_rev", "telemetry_rth_full", "telemetry_rth_ap_link_loss_unknown"])
@pytest.mark.parametrize("proto", [1, 2])
def test_the_ungated_a2_keys_reach_an_older_ground_on_both_link_versions(forward, name, proto):
    assert f"{name} (v{proto})" in forward.compatible


def test_the_gated_v2_only_shapes_are_refused_by_the_base(forward):
    """The base refuses every new type, reply and reason, which shows the gates are needed."""
    refused = {label.split(" (")[0] for label in forward.gated_refused}
    for name in ("describe_unit", "return_home", "set_no_entry", "set_speed", "escort_setpoint",
                 "assign_lanes_on_complete_return_home", "describe_unit_ack", "describe_unit_nack",
                 "ack_return_home", "nack_return_home", "nack_straight_line_home"):
        assert name in refused


def test_only_the_optional_assign_lanes_keys_are_gated_yet_accepted_by_the_base(forward):
    """An older edge would ignore these keys without a refusal, so only the accepts gate keeps them away."""
    accepted = sorted(label.split(" (")[0] for label in forward.gated_accepted)
    assert accepted == ["assign_lanes_corridor", "assign_lanes_landing", "assign_lanes_no_entry",
                        "assign_lanes_speed"]


def test_no_message_from_before_a2_is_gated():
    for sample in compat.common_corpus(fp):
        if sample.name == "nack_takeoff_straight_line_home":
            continue
        assert compat.gate_of(sample.msg) is None, sample.name


def test_the_corpus_covers_the_reviewed_shapes():
    names = {sample.name for sample in compat.corpus()}
    assert {"manual_move", "set_target", "stream_video", "detections", "ack_takeoff", "ack_resume",
            "ack_nudge", "ack_handover", "ack_emergency_land_all", "nack_hold_straight_line_home",
            "nack_assign_lanes_straight_line_home", "nack_manual_takeoff_straight_line_home"} <= names


def test_the_older_ground_gets_invalid_instead_of_straight_line_home():
    assert fp.nack_reason_for_peer("straight_line_home", None) == "invalid"
    assert fp.nack_reason_for_peer("straight_line_home", fp.make_describe_unit("drone-1")) == "straight_line_home"


def test_the_mobile_contract_fixtures_reach_an_older_peer(base):
    report = compat.check_forward(base, compat.fixture_samples())
    assert report.incompatible == [] and report.unsendable == []
    assert report.gated_refused == [] and report.gated_accepted == []


def test_the_real_edges_worst_case_telemetry_with_rth_reaches_an_older_ground(base):
    from tests.test_telemetry_field_cap import _GpsTello, _NoGpsTello, _wire

    rth = fp.make_rth_object("rth_returning", reason="critical_battery", home=(0, 0), distance_cm=900,
                             uncertainty_cm=40, reserve_pct=30, threshold_pct=25, countdown_s=3,
                             cannot_recall=False, landing_area_id="pad-1", landing_place=1,
                             ap_link_loss="unknown")
    samples = [
        compat.Sample(f"worst_{cls.__name__}", fp.DIRECTION_EDGE_TO_GROUND, {**_wire(cls), "rth": rth})
        for cls in (_GpsTello, _NoGpsTello)
    ]
    report = compat.check_forward(base, samples)
    assert report.incompatible == [] and report.unsendable == []
    assert sorted(report.compatible) == sorted(f"{s.name} (v{p})" for s in samples for p in (1, 2))


# --- reverse: an older peer's messages reach today's code -------------------


def _reverse_failures(base, samples):
    failures = []
    for sample in samples:
        for proto in compat.PEER_PROTOS:
            try:
                shaped = base.shape_for_peer(sample.msg, proto)
                text = base.encode(shaped, direction=sample.direction, proto=proto)
            except base.ProtocolError:
                continue
            refusal = compat._accepts(fp, text, sample.direction, proto)
            if refusal is None and sample.direction == fp.DIRECTION_GROUND_TO_EDGE and proto >= 2:
                if compat._accepts(base, text, sample.direction, proto, require_fence=True) is None:
                    refusal = compat._accepts(fp, text, sample.direction, proto, require_fence=True)
            if refusal is not None:
                failures.append(f"{sample.name} (v{proto}): {refusal}")
    return failures


def test_every_message_the_base_builders_produce_is_accepted_today(base):
    samples = compat.common_corpus(base)
    assert len(samples) > 70
    assert _reverse_failures(base, samples) == []


def test_the_fixtures_sent_by_an_older_peer_are_accepted_today(base):
    assert _reverse_failures(base, compat.fixture_samples()) == []


def test_todays_builders_with_the_old_arguments_are_byte_identical(base):
    old = {s.name: json.dumps(s.msg) for s in compat.common_corpus(base)}
    new = {s.name: json.dumps(s.msg) for s in compat.common_corpus(fp)}
    # The only extra sample is the new nack reason, which the base has no constant for.
    assert set(new) - set(old) == {"nack_takeoff_straight_line_home"}
    assert {name: new[name] for name in old} == old


def test_the_protocol_versions_are_unchanged(base):
    assert (fp.PROTOCOL_VERSION, fp.MIN_PROTOCOL_VERSION) == (base.PROTOCOL_VERSION, base.MIN_PROTOCOL_VERSION) == (2, 1)


# --- shape_for_peer: a v1 peer never sees an A2 key -------------------------


_A2_KEYS = {"unit_rev", "rth", "ap_link_loss", "corridor", "landing", "no_entry", "speed_cm_s", "accepts"}


def _keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _keys(item)


@pytest.mark.parametrize("sample", compat.a2_corpus(), ids=lambda s: s.name)
def test_a_v1_peer_gets_no_a2_key_or_type(sample):
    try:
        shaped = fp.shape_for_peer(sample.msg, 1)
    except fp.ProtocolError:
        return  # refused for a v1 peer, which is the other allowed outcome
    assert not _A2_KEYS & set(_keys(shaped)), sample.name
    assert shaped["type"] not in fp.RETURN_COMMAND_TYPES | {fp.DESCRIBE_UNIT}
    assert compat.gate_of(shaped) is None, sample.name


def test_unit_rev_and_rth_are_stripped_not_refused_for_a_v1_peer():
    hello = fp.make_edge_hello("drone-1", "edge-1", 7, plan_id=None, lane_index=None, is_flying=False,
                               battery=80, unit_rev=compat.REV)
    assert "unit_rev" not in fp.shape_for_peer(hello, 1)
    v2 = TelemetryV2(epoch=7, plan_id="p-1", state="flying_lanes", plan_valid=True)
    tel = fp.make_telemetry("drone-1", 82, 100, True, 1.0, 2.0, 0.0, 0, 4, 0.1, None, v2=v2,
                            rth=fp.make_rth_object("flying", ap_link_loss="return"))
    assert "rth" not in fp.shape_for_peer(tel, 1)


# --- the script ---------------------------------------------------------------


def test_the_script_prints_the_summary_and_exits_zero(base, capsys):
    assert compat.main(["--base", BASE]) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("compatible: ") and ", gated: " in last
    assert last.endswith(" accepted by base but stripped of meaning (now refused by the send path)")


def test_the_script_leaves_no_base_module_behind(base):
    with compat.base_package(BASE) as module:
        name = module.__name__
        directory = Path(module.__file__).parent
    assert name not in sys.modules
    assert not directory.exists()


# --- the send path enforces the accepts gate ---------------------------------


_GATED_KEYS = {
    "corridor": {"corridor": [{"x_cm": 0, "y_cm": 0}, {"x_cm": 300, "y_cm": 0}, {"x_cm": 300, "y_cm": 300}]},
    "landing": {"landing": {"area_id": "pad-1", "place": 0, "interval_s": 5}},
    "no_entry": {"no_entry": []},
    "speed_cm_s": {"speed_cm_s": 150},
}


def _plan(**extra):
    return fp.make_assign_lanes(
        "drone-1", 100, [{"direction": "forward", "distance_cm": 200}], {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400},
        fence=fp.Fence(7, "ground-a"), cmd_id="c-al", plan_id="p-1", plan_seq=3, **extra)


def _proof(*names):
    return compat._describe_unit_ack(fp, list(names))


@pytest.mark.parametrize("key", sorted(_GATED_KEYS))
def test_the_send_path_refuses_a_gated_key_for_a_v2_peer_without_proof(key):
    msg = _plan(**_GATED_KEYS[key])
    with pytest.raises(fp.ProtocolError, match="has not proved"):
        fp.shape_for_peer(msg, 2)
    with pytest.raises(fp.ProtocolError, match="has not proved"):
        fp.shape_for_peer(msg, 2, peer_description=_proof("return_home"))


@pytest.mark.parametrize("key", sorted(_GATED_KEYS))
def test_the_send_path_refuses_a_gated_key_for_a_v1_peer_even_with_proof(key):
    msg = _plan(**_GATED_KEYS[key])
    for description in (None, _proof(f"assign_lanes.{key}")):
        with pytest.raises(fp.ProtocolError):
            fp.shape_for_peer(msg, 1, peer_description=description)


@pytest.mark.parametrize("key", sorted(_GATED_KEYS))
def test_the_send_path_allows_a_gated_key_with_proof(key):
    msg = _plan(**_GATED_KEYS[key])
    assert fp.shape_for_peer(msg, 2, peer_description=_proof(f"assign_lanes.{key}")) == msg


def test_the_send_path_needs_proof_for_every_key_the_message_carries():
    msg = _plan(**_GATED_KEYS["corridor"], **_GATED_KEYS["speed_cm_s"])
    with pytest.raises(fp.ProtocolError):
        fp.shape_for_peer(msg, 2, peer_description=_proof("assign_lanes.corridor"))
    proof = _proof("assign_lanes.corridor", "assign_lanes.speed_cm_s")
    assert fp.shape_for_peer(msg, 2, peer_description=proof) == msg


def test_the_send_path_refuses_on_complete_return_home_without_proof():
    msg = _plan(on_complete="return_home", hover_timeout_s=5)
    with pytest.raises(fp.ProtocolError):
        fp.shape_for_peer(msg, 2)
    proof = _proof("assign_lanes.on_complete.return_home")
    assert fp.shape_for_peer(msg, 2, peer_description=proof) == msg


@pytest.mark.parametrize("sample", [s for s in compat.common_corpus(fp) if s.msg.get("type") == "assign_lanes"],
                         ids=lambda s: s.name)
@pytest.mark.parametrize("proto", [1, 2])
def test_an_assign_lanes_without_the_new_keys_is_byte_identical(sample, proto, base):
    """Today's plans are unchanged by the gate: same bytes as the base send path, with or without proof."""
    try:
        want = json.dumps(base.shape_for_peer(sample.msg, proto), separators=(",", ":"))
    except base.ProtocolError:
        want = None  # the base refuses it for this link (an unfenced plan on v2), so must today
    for description in (None, _proof(*sorted(fp.ACCEPT_NAMES - {"escort_setpoint"}))):
        if want is None:
            with pytest.raises(fp.ProtocolError):
                fp.shape_for_peer(sample.msg, proto, peer_description=description)
            continue
        got = fp.shape_for_peer(sample.msg, proto, peer_description=description)
        assert json.dumps(got, separators=(",", ":")) == want
