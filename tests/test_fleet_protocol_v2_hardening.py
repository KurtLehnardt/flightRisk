"""Tests for the review fixes to fleet protocol v2 (PR #96).

Each section covers one review finding:

* the fail-safe landing path through ``decode``, including a differential
  check against the pre-v2 edge's parser (``json.loads`` in ``transport.py``);
* privacy enforcement: allowlists on the peer link and the hardened checks on
  the edge links;
* ``resume`` carrying ``plan_id``, plan ordering with ``plan_seq`` and the
  automatic retry set;
* the primitives a v2-only deployment uses to refuse v1;
* ``handover`` epochs and ``next_epoch``.

Everything here is pure; no socket, drone or file is touched.
"""

from __future__ import annotations

import json
import random

import pytest

from flightrisk import fleet_protocol as fp

G2E = fp.DIRECTION_GROUND_TO_EDGE
E2G = fp.DIRECTION_EDGE_TO_GROUND
PEER = fp.DIRECTION_PEER

# The ``websockets`` library's default frame limit, which transport.py uses:
# the pre-v2 edge could never receive a larger message.
WEBSOCKET_MAX_SIZE = 2**20


# ===========================================================================
# Fail-safe landing through decode
# ===========================================================================


def pre_v2_edge_landing(raw) -> str | None:
    """The landing the pre-v2 edge executed for *raw*, or ``None``.

    This mirrors ``EdgeTransport._read_loop`` and ``_dispatch_incoming`` on
    main: ``json.loads(raw)``, ignore anything that raises or is not an
    object, then act on ``data.get("type")``. The WebSocket layer refused
    frames over ``WEBSOCKET_MAX_SIZE`` bytes before that code ran.
    """
    size = len(raw) if isinstance(raw, (bytes, bytearray)) else len(raw.encode("utf-8", "surrogatepass"))
    if size > WEBSOCKET_MAX_SIZE:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    mtype = data.get("type")
    return mtype if isinstance(mtype, str) and mtype in fp.FAILSAFE_TYPES else None


@pytest.mark.parametrize("raw, expected", [
    ('{"type":"land","cmd_id":"a","cmd_id":"b"}', "land"),
    ('{"type":"land","type":"land"}', "land"),
    ('{"type":"land","x":NaN}', "land"),
    ('{"type":"land","x":Infinity,"y":-Infinity}', "land"),
    ('{"type":"land","x":1e400}', "land"),
    ('{"type":"land","x":' + "7" * 5000 + "}", "land"),
    ('{"type":"land","x":"tab\there"}', "land"),
    (json.dumps({"type": "emergency_land_all", "pad": "a" * 70_000}), "emergency_land_all"),
    (json.dumps({"type": "land", "pad": "a" * (fp.FAILSAFE_MAX_BYTES - 100)}), "land"),
    (b'{"type":"land","x":"\xff"}', "land"),
    (b'\xef\xbb\xbf{"type":"land"}', "land"),
    ('﻿{"type":"land"}', "land"),
    ('{"type":"land"}'.encode("utf-16"), "land"),
    ('{"type":"emergency_land_all"}'.encode("utf-32-be"), "emergency_land_all"),
    # A repeated type counts if any occurrence is a landing: the safe reading.
    ('{"type":"heartbeat","seq":1,"type":"land"}', "land"),
    ('{"type":"land","type":"heartbeat"}', "land"),
    ('{"type":"takeoff","type":"emergency_land_all","type":"land"}', "emergency_land_all"),
], ids=lambda value: repr(value[:40]) if isinstance(value, (str, bytes)) else None)
def test_decode_accepts_landings_the_strict_parser_rejects(raw, expected):
    for proto in (1, 2):
        assert fp.decode(raw, direction=G2E, proto=proto) == {"type": expected}
    assert fp.failsafe_type_of(raw) == expected


@pytest.mark.parametrize("raw", [
    '{"type":"heartbeat","type":"takeoff"}',
    '{"type":"takeoff","x":NaN}',
    '{"type":["land"],"x":NaN}',
    '[{"type":"land"}]',
    '{"x":{"type":"land"},"x":1}',
    '"land"',
    '{"type":"LAND","x":NaN}',
    '{"type":"land"} trailing',
    '{"type":"land","x":' + "[" * 100_000 + "]" * 100_000 + "}",
    json.dumps({"type": "land", "pad": "a" * fp.FAILSAFE_MAX_BYTES}),
    b"\xff" * 10,
    "",
], ids=lambda raw: repr(raw[:40]))
def test_fail_safe_fallback_refuses_everything_that_is_not_a_landing(raw):
    with pytest.raises(fp.ProtocolError):
        fp.decode(raw, direction=G2E, proto=2)
    assert fp.failsafe_type_of(raw) is None


def test_fail_safe_fallback_applies_only_to_ground_to_edge():
    raw = '{"type":"land","x":NaN}'
    for direction in (E2G, PEER):
        with pytest.raises(fp.ProtocolError):
            fp.decode(raw, direction=direction, proto=2)


def test_strictly_valid_landing_is_returned_whole():
    msg = {"type": "land", "cmd_id": "c-1", "epoch": 3, "leader_id": "mac"}
    assert fp.decode(json.dumps(msg), direction=G2E, proto=2) == msg


def test_decode_fallback_bound_follows_a_larger_max_bytes():
    raw = json.dumps({"type": "land", "pad": "a" * (fp.FAILSAFE_MAX_BYTES + 10)})
    with pytest.raises(fp.ProtocolError):
        fp.decode(raw, direction=G2E, proto=2)
    assert fp.decode(raw, direction=G2E, proto=2, max_bytes=2 * fp.FAILSAFE_MAX_BYTES)["type"] == "land"


@pytest.mark.parametrize("value", [None, 0, -1, "64", 1.5, True])
def test_bad_max_bytes_is_a_protocol_error(value):
    with pytest.raises(fp.ProtocolError):
        fp.decode('{"type":"land"}', direction=G2E, proto=2, max_bytes=value)
    assert fp.failsafe_type_of('{"type":"land"}', max_bytes=value) is None


_EXTRA_VALUES = [
    "NaN", "Infinity", "-Infinity", "1e400", "-1e400", "1" * 4400, "0", "-0.0", "true", "null",
    '"x"', '"\\ud800"', '"tab\tin string"', '"\\u0000"', "[]", "{}", '{"a":NaN,"a":1}',
    "[" * 40 + "1" + "]" * 40, '"' + "b" * 300 + '"',
]
_TYPE_VALUES = ['"land"', '"emergency_land_all"', '"heartbeat"', '"takeoff"', '"hold"', "null", '["land"]', "7"]


def _random_landing_text(rng: random.Random) -> str:
    """A JSON-ish object built by hand, so keys can repeat and constants appear."""
    pairs = [("type", rng.choice(['"land"', '"emergency_land_all"']))]
    for _ in range(rng.randrange(6)):
        key = rng.choice(["type", "cmd_id", "epoch", "leader_id", "x", "face", "pad"])
        value = rng.choice(_TYPE_VALUES) if key == "type" else rng.choice(_EXTRA_VALUES)
        pairs.insert(rng.randrange(len(pairs) + 1), (key, value))
    if rng.random() < 0.1:
        pairs.append(("pad", '"' + "p" * rng.choice([70_000, 300_000, 1_100_000]) + '"'))
    space = rng.choice(["", " ", "\n  "])
    return "{" + ",".join(f'{space}"{k}"{space}:{space}{v}' for k, v in pairs) + space + "}"


def _encodings(rng: random.Random, text: str) -> list:
    out: list = [text]
    try:
        out.append(text.encode("utf-8"))
    except UnicodeEncodeError:
        return out
    out.append(rng.choice([
        b"\xef\xbb\xbf" + text.encode("utf-8"),
        text.encode("utf-16"),
        text.encode("utf-16-le"),
        text.encode("utf-32"),
        text.encode("utf-8") + b"\xff",
    ]))
    return out


def _byte_mutation(rng: random.Random, text: str) -> str:
    chars = list(text)
    for _ in range(rng.randrange(1, 4)):
        pos = rng.randrange(len(chars) + 1)
        op = rng.randrange(3)
        if op == 0:
            chars.insert(pos, rng.choice(list(' ,:{}[]"\\nNaI0e-') + ["\x00", "﻿", "\ud800"]))
        elif op == 1 and pos < len(chars):
            del chars[pos]
        elif pos < len(chars):
            chars[pos] = rng.choice(list(' ,:{}"0aN'))
    return "".join(chars)


@pytest.mark.parametrize("seed", range(4))
def test_differential_every_pre_v2_landing_is_still_a_landing(seed):
    """Every input the pre-v2 edge executed as a landing, decode also returns as one."""
    rng = random.Random(4000 + seed)
    via_fallback = 0
    checked = 0
    for _ in range(1500):
        text = _random_landing_text(rng)
        if rng.random() < 0.4:
            text = _byte_mutation(rng, text)
        for raw in _encodings(rng, text):
            expected = pre_v2_edge_landing(raw)
            if expected is None:
                continue
            checked += 1
            for proto in (1, 2):
                got = fp.decode(raw, direction=G2E, proto=proto)["type"]
                assert got in fp.FAILSAFE_TYPES, raw
                if expected == fp.EMERGENCY_LAND_ALL:
                    assert got == fp.EMERGENCY_LAND_ALL, raw
            if not _strictly_parses(raw):
                via_fallback += 1
    # The sweep must exercise the fallback, not only strictly valid landings.
    assert checked > 1000
    assert via_fallback > 300


def _strictly_parses(raw) -> bool:
    try:
        fp._parse_strict(raw, fp.MAX_MESSAGE_BYTES)
    except fp.ProtocolError:
        return False
    return True


@pytest.mark.parametrize("seed", range(4))
def test_fail_safe_fallback_never_raises_on_random_bytes(seed):
    rng = random.Random(5000 + seed)
    for _ in range(2000):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(40)))
        if rng.random() < 0.5:
            raw = b'{"type":"land"' + raw
        assert fp.failsafe_type_of(raw) in (None, "land", "emergency_land_all")
        assert fp.failsafe_type_of(raw.decode("latin-1")) in (None, "land", "emergency_land_all")
        try:
            result = fp.decode(raw, direction=G2E, proto=2)
        except fp.ProtocolError:
            continue
        assert isinstance(result, dict)


# ===========================================================================
# Privacy: peer allowlists and the edge-link screen
# ===========================================================================

FENCE = fp.Fence(7, "mac")


def _node_state():
    return fp.make_node_state(
        "mac",
        "leader",
        7,
        roster=[{"drone_id": "drone-1", "node_id": "mac", "connected": True, "is_flying": False,
                 "epoch_seen": 7}],
        mission={"plan_id": "p-1", "pattern": "split_expanding_square", "state": "idle",
                 "area_cm": [2000, 2000], "lkp_cm": [0, 0],
                 "placements": {"drone-1": [-50, 0, 0], "drone-2": [50, 0, 0]}},
        latch={"phase": "searching", "alert_id": None},
        detections=[{"id": 3, "x_cm": 120, "y_cm": -40, "drone_ids": ["drone-2"], "first_seen_s": 81.2}],
    )


def _telemetry():
    return fp.make_telemetry("drone-1", 87, 160, True, 10.0, -20.0, 90.0, 1, 4, 0.2, None)


# Field names an innocuous-looking extra field might use. None is private, so
# only the allowlist can refuse them.
BENIGN_EXTRAS = ["notes", "x", "meta", "target", "blob"]


@pytest.mark.parametrize("key", BENIGN_EXTRAS)
def test_peer_allowlist_refuses_unknown_fields_at_every_level(key):
    def where(mutate):
        msg = _node_state()
        mutate(msg)
        with pytest.raises(fp.ProtocolError, match="not in its schema"):
            fp.validate_peer_message(msg)
        with pytest.raises(fp.ProtocolError, match="not in its schema"):
            fp.validate_peer_message({"type": "handover_offer", "epoch_next": 8, "snapshot": msg})

    where(lambda m: m.__setitem__(key, 1))
    where(lambda m: m["roster"][0].__setitem__(key, 1))
    where(lambda m: m["mission"].__setitem__(key, 1))
    where(lambda m: m["latch"].__setitem__(key, 1))
    where(lambda m: m["detections"][0].__setitem__(key, 1))
    for msg in (fp.make_handover_accept(8), fp.make_handover_reject("busy"), fp.make_peer_heartbeat(1, 7),
                fp.make_handover_offer(8, _node_state())):
        with pytest.raises(fp.ProtocolError, match="not in its schema"):
            fp.validate_peer_message(dict(msg, **{key: 1}))


def test_peer_strings_are_capped_at_max_text_len():
    msg = _node_state()
    msg["mission"]["placements"]["d" * (fp.MAX_TEXT_LEN + 1)] = [0, 0, 0]
    with pytest.raises(fp.ProtocolError):
        fp.validate_peer_message(msg)
    text = ("busy " * fp.MAX_TEXT_LEN)[: fp.MAX_TEXT_LEN]
    assert fp.validate_peer_message(fp.make_handover_reject(text)) == "handover_reject"
    with pytest.raises(fp.ProtocolError):
        fp.make_handover_reject(text + "x")


# The bypasses the review found, plus spelling variants of them.
PRIVATE_KEYS = [
    "image_b64", "img", "crop_path", "crop_file", "reid", "target_embedding", "faceEmbedding", "thumb",
    "jpeg_b64", "vector", "Embedding", "ＦＡＣＥ", "target_vec", "reid_vec", "Face-Embedding", "f.a.c.e",
    "IMAGE", "thumbnail_jpeg", "crop_jpeg", "featureVector", "ｉｍａｇｅ＿ｂ６４", "photoURL", "pixels",
]


@pytest.mark.parametrize("key", PRIVATE_KEYS)
def test_private_keys_are_refused_on_every_link(key):
    with pytest.raises(fp.ProtocolError, match="imagery or biometric"):
        fp.validate_message(dict(_telemetry(), **{key: "x"}))
    with pytest.raises(fp.ProtocolError, match="imagery or biometric"):
        fp.validate_message(dict(fp.make_hold(FENCE), meta=[{"inner": {key: 1}}]), proto=2)
    state = _node_state()
    state["detections"][0][key] = "x"
    with pytest.raises(fp.ProtocolError):
        fp.validate_peer_message(state)


@pytest.mark.parametrize("value", [
    "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ",
    "DATA:image/png,abc",
    "prefix ;base64, suffix",
    "A" * (fp.MAX_ID_LEN + 1),
    "0123456789abcdef" * 5,
    ("QUJD" * 19 + "\n") * 3,
    "/9j/" + "a+b/" * 40 + "==",
], ids=lambda value: repr(value[:24]))
def test_encoded_media_strings_are_refused(value):
    with pytest.raises(fp.ProtocolError, match="encoded media"):
        fp.validate_message(dict(_telemetry(), note=value))
    with pytest.raises(fp.ProtocolError, match="encoded media"):
        fp.validate_message(dict(fp.make_nudge(FENCE, "left", 50), extra={"k": [value]}), proto=2)
    with pytest.raises(fp.ProtocolError):
        fp.validate_peer_message(dict(_node_state(), node_id=value))


def test_ordinary_text_is_not_mistaken_for_media():
    sentence = "drone two refused its lanes because the geofence check failed at the far edge of the zone"
    assert fp.validate_message(dict(_telemetry(), note=sentence)) == "telemetry"
    assert fp.validate_message(dict(_telemetry(), note="A" * fp.MAX_ID_LEN)) == "telemetry"
    nack = fp.make_nack("hold", "drone-1", "busy", "c-1", detail="worker running; retry after the current lane")
    assert fp.validate_message(nack, proto=2) == "nack"


def test_number_lists_that_could_carry_an_embedding_are_refused():
    ok = [0.5] * fp.MAX_NUMBERS_PER_LIST
    assert fp.validate_message(dict(_telemetry(), samples=ok)) == "telemetry"
    for bad in ([0.5] * (fp.MAX_NUMBERS_PER_LIST + 1), [0.1] * 512, list(range(128)),
                [[0.5] * fp.MAX_NUMBERS_PER_LIST] * 5, {"a": [[1] * 16] * 3, "b": [[2] * 16] * 2}):
        with pytest.raises(fp.ProtocolError, match="numbers"):
            fp.validate_message(dict(_telemetry(), samples=bad))
    roster_vec = _node_state()
    roster_vec["roster"][0]["epoch_seen"] = [0.1] * 512
    with pytest.raises(fp.ProtocolError):
        fp.validate_peer_message(roster_vec)


def test_binary_values_are_refused():
    for value in (b"\xff\xd8\xff", bytearray(b"x"), memoryview(b"x")):
        with pytest.raises(fp.ProtocolError, match="binary"):
            fp.validate_message(dict(_telemetry(), blob=value))


def test_every_schema_field_passes_the_privacy_screen():
    """No legitimate field name or value trips the denylist or the heuristics."""
    lanes = [{"direction": "forward", "distance_cm": 200}]
    zone = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}
    messages = [
        (fp.make_assign_lanes("drone-1", 100, lanes, zone, fence=FENCE, plan_id="p-1", on_complete="hover"), 2),
        (fp.make_start_at(1.0, fence=FENCE, plan_id="p-1"), 2),
        (fp.make_handover(FENCE, "win"), 2),
        (fp.make_edge_hello("drone-1", "win", 7, plan_id="p-1", lane_index=0, is_flying=True, battery=50), 2),
        (fp.make_telemetry("drone-1", 1, 1, True, 1, 1, 1, 1, 1, 1, 1, v2=fp.TelemetryV2(7, "p", "idle", True)), 2),
        (fp.make_nack("assign_lanes", "drone-1", "busy", fp.new_cmd_id(), lanes_total=1, epoch_seen=7), 2),
        (fp.make_ack_start_at("drone-1", 1.0, cmd_id=fp.new_cmd_id()), 2),
        (_node_state(), 2),
        (fp.make_handover_offer(8, _node_state()), 2),
    ]
    for msg, proto in messages:
        assert fp.validate_message(msg, proto=proto) == msg["type"]
