#!/usr/bin/env python3
"""RTP/H.264 vectors for the phone video path (plan section 2.6, "Video").

Writes ``tests/fixtures/mission_core/rtp_vectors.json``: RTP packets made by an RFC 6184 packetizer over
synthetic H.264 NAL units, and the NAL units a depacketizer must produce from them. MAVLink drones in v1 send
H.264 over RTP on UDP port 5600 plus the drone index (QGroundControl's convention). The depacketizers of both
apps (Kotlin ``RtpH264``, Swift ``RtpH264``) must pass every case.

Rules the cases pin (RFC 3550 for the RTP header, RFC 6184 for the payload):

* Single NAL unit packets (NAL types 1 to 23) give their payload as one NAL unit.
* FU-A (type 28) fragments are joined in sequence-number order; the NAL header is rebuilt from the FU
  indicator's F and NRI bits and the FU header's type. A gap in sequence numbers, a change of timestamp, a
  packet of another kind before the end fragment, or a fragment without a start drops the unfinished NAL.
* STAP-A (type 24) is dropped and counted; v1 senders (GStreamer ``rtph264pay``, as QGroundControl expects)
  send parameter sets as single NAL packets.
* STAP-B, MTAP16, MTAP24, FU-B (25, 26, 27, 29) and the undefined types 0, 30 and 31 are dropped as unsupported.
* The header's CSRC list, header extension and padding are skipped; a packet that is not RTP version 2, too
  short, or whose padding exceeds its payload is invalid.
* Sequence numbers wrap from 65535 to 0 without a gap. Payload type and SSRC are not checked.

Interface for ``scripts/make_mission_core_vectors.py``: ``OUTPUTS`` and ``render`` as in ``mavlink_frames.py``.
Run with ``--check`` or ``--write``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "tests" / "fixtures" / "mission_core"
GENERATOR = "scripts/mission_vectors/rtp.py"

MTU_PAYLOAD = 1200
PT = 96
SSRC = 0x1234ABCD
FRAME_TICKS = 3000  # 90 kHz clock at 30 frames per second.
COUNTERS = ("stap_a_dropped", "fu_a_dropped", "unsupported_dropped", "invalid_dropped")


def _bytes(seed: int, n: int) -> bytes:
    """Deterministic filler (a 32-bit LCG); never 0x00 0x00 so no start code can appear by accident."""
    out = bytearray()
    x = seed & 0xFFFFFFFF
    while len(out) < n:
        x = (1664525 * x + 1013904223) & 0xFFFFFFFF
        out.append(((x >> 24) % 254) + 1)
    return bytes(out)


def nal(header: int, size: int, seed: int) -> bytes:
    return bytes([header]) + _bytes(seed, size - 1)


SPS = nal(0x67, 14, 1)
PPS = nal(0x68, 5, 2)
SEI = nal(0x06, 21, 3)
IDR_SMALL = nal(0x65, 800, 4)
IDR_LARGE = nal(0x65, 2600, 5)
SLICE = nal(0x41, 600, 6)
SLICE_LARGE = nal(0x41, 1500, 7)


def rtp(seq: int, ts: int, payload: bytes, *, marker: bool = False, version: int = 2, csrcs=(), ext: bytes | None = None,
        padding: int = 0) -> bytes:
    b0 = (version << 6) | (0x20 if padding else 0) | (0x10 if ext is not None else 0) | len(csrcs)
    b1 = (0x80 if marker else 0) | PT
    head = bytes([b0, b1]) + (seq & 0xFFFF).to_bytes(2, "big") + ts.to_bytes(4, "big") + SSRC.to_bytes(4, "big")
    for c in csrcs:
        head += c.to_bytes(4, "big")
    if ext is not None:
        head += (0xBEDE).to_bytes(2, "big") + (len(ext) // 4).to_bytes(2, "big") + ext
    tail = (bytes(padding - 1) + bytes([padding])) if padding else b""
    return head + payload + tail


def fu_a(unit: bytes, size: int = MTU_PAYLOAD) -> list[bytes]:
    indicator = (unit[0] & 0xE0) | 28
    body = unit[1:]
    chunk = size - 2
    parts = [body[i:i + chunk] for i in range(0, len(body), chunk)]
    out = []
    for i, part in enumerate(parts):
        header = (0x80 if i == 0 else 0) | (0x40 if i == len(parts) - 1 else 0) | (unit[0] & 0x1F)
        out.append(bytes([indicator, header]) + part)
    return out


def payloads(unit: bytes) -> list[bytes]:
    return [unit] if len(unit) <= MTU_PAYLOAD else fu_a(unit)


def stap_a(units: list[bytes]) -> bytes:
    nri = max(u[0] & 0x60 for u in units)
    out = bytes([nri | 24])
    for u in units:
        out += len(u).to_bytes(2, "big") + u
    return out


class Stream:
    """Packetizes access units; records the expected NAL output as it goes."""

    def __init__(self, seq: int = 1000, ts: int = 90000):
        self.seq = seq
        self.ts = ts
        self.packets: list[dict] = []
        self.expect: list[dict] = []

    def packet(self, payload: bytes, *, marker=False, note=None, **kw):
        record = {"hex": rtp(self.seq, self.ts, payload, marker=marker, **kw).hex()}
        if note:
            record["note"] = note
        self.packets.append(record)
        self.seq = (self.seq + 1) & 0xFFFF

    def access_unit(self, units: list[bytes], *, expect=True):
        for i, unit in enumerate(units):
            parts = payloads(unit)
            for j, part in enumerate(parts):
                last = i == len(units) - 1 and j == len(parts) - 1
                self.packet(part, marker=last)
            if expect:
                self.expect.append({"ts": self.ts, "au_end": i == len(units) - 1, "nal": unit.hex()})
        self.ts += FRAME_TICKS

    def skip(self, n: int = 1):
        self.seq = (self.seq + n) & 0xFFFF


def _case(name, text, stream, **counters):
    return {
        "name": name,
        "text": text,
        "packets": stream.packets,
        "nals": stream.expect,
        "counters": {c: counters.get(c, 0) for c in COUNTERS},
    }


def build() -> dict:
    cases = []

    s = Stream()
    s.access_unit([SPS, PPS, SEI, IDR_SMALL])
    s.access_unit([SLICE])
    cases.append(_case("single_nal_units", "Parameter sets, SEI and slices that fit one packet each.", s))

    s = Stream()
    s.access_unit([SPS, PPS, IDR_LARGE])
    s.access_unit([SLICE_LARGE])
    cases.append(_case("fu_a", "A 2600-byte IDR in three FU-A fragments and a 1500-byte slice in two.", s))

    s = Stream()
    s.packet(stap_a([SPS, PPS]), note="STAP-A with SPS and PPS: dropped")
    s.access_unit([IDR_SMALL])
    cases.append(_case("stap_a_dropped", "STAP-A is dropped and counted; the following IDR still comes out.", s,
                       stap_a_dropped=1))

    s = Stream()
    frags = fu_a(IDR_LARGE)
    s.packet(frags[0])
    s.skip()
    s.packet(frags[2], marker=True, note="the middle fragment was lost")
    s.ts += FRAME_TICKS
    s.access_unit([SLICE])
    cases.append(_case("fu_a_gap", "A lost middle fragment drops the whole NAL (start and end fragments).", s,
                       fu_a_dropped=2))

    s = Stream()
    frags = fu_a(IDR_LARGE)
    s.packet(frags[1], note="no start fragment")
    s.packet(frags[2], marker=True)
    s.ts += FRAME_TICKS
    s.access_unit([SLICE])
    cases.append(_case("fu_a_without_start", "Fragments that arrive without their start are dropped.", s,
                       fu_a_dropped=2))

    s = Stream()
    frags = fu_a(IDR_LARGE)
    s.packet(frags[0])
    s.packet(frags[1])
    s.ts += FRAME_TICKS
    s.packet(frags[2], marker=True, note="timestamp changed before the end fragment")
    s.ts += FRAME_TICKS
    s.access_unit([SLICE])
    cases.append(_case("fu_a_timestamp_change", "A timestamp change inside a fragmented NAL drops it.", s,
                       fu_a_dropped=3))

    s = Stream()
    frags = fu_a(IDR_LARGE)
    s.packet(frags[0])
    unit_ts = s.ts
    s.packet(SEI, note="a single NAL before the end fragment")
    s.expect.append({"ts": unit_ts, "au_end": False, "nal": SEI.hex()})
    s.packet(frags[1])
    s.packet(frags[2], marker=True)
    s.ts += FRAME_TICKS
    cases.append(_case("fu_a_interrupted", "Another packet inside a fragmented NAL drops the unfinished NAL and the "
                       "fragments after it; the other packet's NAL comes out.", s, fu_a_dropped=3))

    s = Stream(seq=65533)
    s.access_unit([SPS, PPS, IDR_LARGE])
    s.access_unit([SLICE])
    cases.append(_case("sequence_wrap", "Sequence numbers 65533 to 65535 and on to 1 are contiguous; the FU-A "
                       "across the wrap is joined.", s))

    s = Stream()
    s.packet(SPS, csrcs=(0x01020304, 0x05060708), note="two CSRCs")
    s.expect.append({"ts": s.ts, "au_end": False, "nal": SPS.hex()})
    s.packet(PPS, ext=bytes([0x10, 0xAA, 0, 0]), note="header extension, one word")
    s.expect.append({"ts": s.ts, "au_end": False, "nal": PPS.hex()})
    s.packet(IDR_SMALL, marker=True, padding=4, note="four bytes of padding")
    s.expect.append({"ts": s.ts, "au_end": True, "nal": IDR_SMALL.hex()})
    s.ts += FRAME_TICKS
    cases.append(_case("header_variants", "CSRCs, a header extension and padding are skipped.", s))

    s = Stream()
    s.packet(SPS, version=1, note="RTP version 1: invalid")
    s.packet(b"", note="header only, no payload: invalid")
    s.packets.append({"hex": rtp(s.seq, s.ts, b"")[:8].hex(), "note": "8 bytes: shorter than an RTP header"})
    s.seq += 1
    bad_pad = bytearray(rtp(s.seq, s.ts, SLICE[:3] + bytes([20])))
    bad_pad[0] |= 0x20
    s.packets.append({"hex": bytes(bad_pad).hex(), "note": "padding count 20 exceeds the 4 payload bytes: invalid"})
    s.seq += 1
    s.packet(bytes([0x7D]) + _bytes(8, 10), note="FU-B (29): unsupported")
    s.packet(bytes([0x79]) + _bytes(9, 10), note="STAP-B (25): unsupported")
    s.packet(bytes([0x60]) + _bytes(10, 10), note="NAL type 0: unsupported")
    s.packet(bytes([0x7E]) + _bytes(11, 10), note="NAL type 30: unsupported")
    s.access_unit([SLICE])
    cases.append(_case("dropped_packets", "Invalid RTP packets and unsupported payload types produce nothing.", s,
                       invalid_dropped=4, unsupported_dropped=4))

    return {
        "v": 1,
        "generator": GENERATOR,
        "format": [
            "Each case feeds 'packets' (RTP packets as hex, one UDP datagram each, in this order) to a fresh "
            "depacketizer. It must output exactly 'nals', in order: each NAL unit as hex without a start code, "
            "with the RTP timestamp of its packets and 'au_end' true when the packet that completed it carried "
            "the marker bit (the end of an access unit).",
            "'counters' are the depacketizer's drop counters after the case: stap_a_dropped counts STAP-A "
            "packets; fu_a_dropped counts FU-A packets that end up in no output NAL; unsupported_dropped counts "
            "packets of payload types 0, 25, 26, 27, 29, 30 and 31; invalid_dropped counts packets that are "
            "not valid RTP.",
            "Payload type %d, SSRC %08x, 90 kHz timestamps, %d bytes of payload per packet at most." % (
                PT, SSRC, MTU_PAYLOAD),
        ],
        "cases": cases,
    }


OUTPUTS = {"rtp_vectors.json": build}


def render(obj: dict) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=True, allow_nan=False) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail if the committed file differs from a fresh build")
    mode.add_argument("--write", action="store_true", help="rewrite the file")
    args = parser.parse_args(argv)
    status = 0
    for name, build_fn in OUTPUTS.items():
        path = OUT_DIR / name
        text = render(build_fn())
        if args.write:
            path.write_text(text, encoding="utf-8")
            print("wrote %s" % path.relative_to(ROOT))
        elif not path.exists() or path.read_text(encoding="utf-8") != text:
            print("%s differs from a fresh build" % path.relative_to(ROOT), file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
