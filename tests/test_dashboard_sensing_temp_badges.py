"""The drone card's temperature items and badge, rendered by ``static/ui/ui.js``, and no depth threshold anywhere.

The helpers between the ``obstacle-display`` and ``temp-display`` markers run under Node with stub inputs, the way
``tests/test_fleet_dashboard_obstacle.py`` runs them; the card's wiring is checked as text. The Python side (the
ground passing ``temp_preflight`` through ``fleet_status``, and never a depth threshold) is checked too. The depth
threshold is not an operator setting, so neither the edge's telemetry nor the card shows it. No drone, no network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from flightrisk.drone.temp_preflight import TempLimits, TempReading, judge_reading, temp_telemetry
from tests.test_fleet_dashboard import _started_supervisor
from tests.test_fleet_dashboard_obstacle import _sensing, _telemetry

JS = (Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard" / "static" / "ui" / "ui.js").read_text()
NODE = shutil.which("node")
HARNESS = r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const cases = JSON.parse(process.argv[3]);
const f = new Function('fmt', src + '; return {personCueBadge, tempMeta, tempTakeoffMeta, tempBadge};')(
    (v) => (v === undefined || v === null) ? '--' : (typeof v === 'number' ? Math.round(v * 10) / 10 : v));
console.log(JSON.stringify(cases.map((c) => f[c.fn].apply(null, c.args))));
"""


def _blocks() -> str:
    obstacle = JS[JS.index("// BEGIN obstacle-display"):JS.index("// END obstacle-display")]
    temp = JS[JS.index("// BEGIN temp-display"):JS.index("// END temp-display")]
    return obstacle + "\n" + temp


def _run(tmp_path, calls):
    block = tmp_path / "block.js"
    block.write_text(_blocks())
    harness = tmp_path / "h.js"
    harness.write_text(HARNESS)
    out = subprocess.run([NODE, str(harness), str(block), json.dumps([{"fn": f, "args": a} for f, a in calls])],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _temp(reading=None, limits=None, detail=None, live=None, checked=True):
    """A ``temp_preflight`` object: the check of *reading* (low, high) or unknown, and the *live* reading."""
    limits = limits or TempLimits()
    check = judge_reading(TempReading(*reading) if reading else None, limits, detail=detail) if checked else None
    return temp_telemetry(check, limits, TempReading(*live) if live else None)


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
class TestBrowserHelpers:
    def test_the_temp_item_is_the_live_reading_coloured_by_the_levels(self, tmp_path):
        cool, warm, hot, stale, legacy, none = _run(tmp_path, [
            ("tempMeta", [{"temp_preflight": _temp((91, 93), live=(53, 56))}]),
            ("tempMeta", [{"temp_preflight": _temp(live=(91, 93), checked=False)}]),
            ("tempMeta", [{"temp_preflight": _temp(live=(96, 96), checked=False)}]),
            ("tempMeta", [{"temp_preflight": _temp((53, 56))}]),
            ("tempMeta", [{"temperature": 40.0}]),
            ("tempMeta", [{"battery": 80}]),
        ])
        assert cool == {"text": "53-56 C", "cls": ""}
        assert warm == {"text": "91-93 C", "cls": "warn"}
        assert hot == {"text": "96 C", "cls": "bad"}
        assert stale == {"text": "--", "cls": ""}  # no fresh state packet
        assert legacy == {"text": "40 C", "cls": ""}  # a local drone without the field, as before
        assert none is None

    def test_the_stored_check_is_labelled_at_takeoff(self, tmp_path):
        ok, warn, refuse, unknown, before = _run(tmp_path, [
            ("tempTakeoffMeta", [{"temp_preflight": _temp((53, 56), live=(80, 82))}]),
            ("tempTakeoffMeta", [{"temp_preflight": _temp((91, 93))}]),
            ("tempTakeoffMeta", [{"temp_preflight": _temp((96, 96))}]),
            ("tempTakeoffMeta", [{"temp_preflight": _temp(detail="stale")}]),
            ("tempTakeoffMeta", [{"temp_preflight": _temp(live=(53, 56), checked=False)}]),
        ])
        assert ok == {"text": "53-56 C", "cls": ""}
        assert warn == {"text": "91-93 C", "cls": "warn"}
        assert refuse == {"text": "96 C", "cls": "bad"}
        assert unknown == {"text": "--", "cls": ""}
        assert before is None  # no check before the first takeoff

    def test_the_badge_follows_the_level_of_the_last_check(self, tmp_path):
        ok, warn, refuse, unknown, off, before, absent, junk = _run(tmp_path, [
            ("tempBadge", [{"temp_preflight": _temp((53, 56))}]),
            ("tempBadge", [{"temp_preflight": _temp((91, 93))}]),
            ("tempBadge", [{"temp_preflight": _temp((96, 98))}]),
            ("tempBadge", [{"temp_preflight": _temp(detail="no fresh state packet (newest 1.2 s old)")}]),
            ("tempBadge", [{"temp_preflight": _temp((100, 100), TempLimits(refuse_c=None))}]),
            ("tempBadge", [{"temp_preflight": _temp(live=(99, 99), checked=False)}]),
            ("tempBadge", [{"temperature": 99}]),
            ("tempBadge", [{"temp_preflight": "hot"}]),
        ])
        assert ok is None and before is None and absent is None and junk is None
        assert warn == {"text": "HOT 91-93 C", "cls": "held", "title": warn["title"]}
        assert "warn level of 92 C at the last takeoff; a takeoff is refused at 95 C" in warn["title"]
        assert refuse["text"] == "TOO HOT 96-98 C: takeoff refused" and refuse["cls"] == "held"
        assert "refusal level of 95 C" in refuse["title"]
        assert unknown["text"] == "TEMP unknown" and "(no fresh state packet (newest 1.2 s old))" in unknown["title"]
        assert "never holds a takeoff back" in unknown["title"]
        assert off["text"] == "HOT 100 C" and "the refusal is off" in off["title"]

    def test_the_new_badges_do_not_change_the_person_cue_badge(self, tmp_path):
        (cue,) = _run(tmp_path, [("personCueBadge", [_sensing(
            temp_preflight=_temp((91, 93)), obstacle_person_cue={"person_cue_down": True, "detection_age_s": 0.4})])])
        assert cue["text"] == "PERSON CUE down"


def test_the_card_shows_the_temperature_and_no_depth_threshold():
    # The flight guard badges (refused moves, the hover-only test) follow the temperature badge.
    assert "[senseBadge(d), personCueBadge(d), obstacleStopBadge(d), tempBadge(d), refusalBadge(d), hoverTestBadge(d)]" in JS
    assert "const temp = tempMeta(d);" in JS
    assert "meta.appendChild(metaItem('Temp', temp.text, temp.cls));" in JS
    assert "meta.appendChild(metaItem('Temp at takeoff', atTakeoff.text, atTakeoff.cls));" in JS
    assert "min_safe_depth" not in JS and "DEPTH " not in JS


def test_fleet_status_never_carries_a_depth_threshold():
    supervisor, _, _, _ = _started_supervisor()
    supervisor.coordinator.handle_message("drone-1", _telemetry("drone-1", **_sensing(obstacle_min_safe_depth=0.77)))
    drones = supervisor.status_payload()["drones"]
    assert "obstacle_min_safe_depth" not in drones["drone-1"]
    json.dumps(drones)


@pytest.mark.parametrize("status", [
    {"sensor": "forward_camera", "min_safe_depth": 0.77, "person_cue_down": False},
    {"sensor": "forward_camera", "min_safe_depth": 0.35},
    {"sensor": "fake"},
])
def test_the_edge_never_reports_the_camera_sensors_threshold(status):
    from flightrisk import fleet_protocol as fp
    from tests.test_obstacle_h6_fixes import _CueSensor, _executor

    msg = _executor(_CueSensor(status)).telemetry()
    assert "obstacle_min_safe_depth" not in msg
    fp.validate_edge_to_ground(msg, proto=1)
