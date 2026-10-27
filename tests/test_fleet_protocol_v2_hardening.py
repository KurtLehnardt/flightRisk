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
