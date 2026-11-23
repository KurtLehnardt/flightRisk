"""The drone card's depth threshold badge and temperature check, rendered by ``static/ui/ui.js``.

The helpers between the ``obstacle-display`` and ``temp-display`` markers run under Node with stub inputs, the way
``tests/test_fleet_dashboard_obstacle.py`` runs them; the card's wiring is checked as text. The Python side (the
ground passing ``obstacle_min_safe_depth`` and ``temp_preflight`` through ``fleet_status``) is checked too. No
drone, no network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from flightrisk.drone.temp_preflight import TempLimits, judge_temperature
from tests.test_fleet_dashboard import _started_supervisor
from tests.test_fleet_dashboard_obstacle import _sensing, _telemetry

JS = (Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard" / "static" / "ui" / "ui.js").read_text()
NODE = shutil.which("node")
HARNESS = r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const cases = JSON.parse(process.argv[3]);
const f = new Function('fmt', src + '; return {depthLimitBadge, personCueBadge, tempMeta, tempBadge};')(
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


def _temp(reply, limits=None, failure=None):
    return judge_temperature(reply, limits or TempLimits(), failure=failure).to_telemetry()


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
class TestBrowserHelpers:
    def test_depth_badge_shows_the_threshold_in_force(self, tmp_path):
        field, default, absent, junk, zero = _run(tmp_path, [
            ("depthLimitBadge", [_sensing(obstacle_min_safe_depth=0.77)]),
            ("depthLimitBadge", [_sensing(obstacle_min_safe_depth=0.35)]),
            ("depthLimitBadge", [_sensing()]),
            ("depthLimitBadge", [_sensing(obstacle_min_safe_depth="0.77")]),
            ("depthLimitBadge", [_sensing(obstacle_min_safe_depth=0)]),
        ])
        assert field["text"] == "DEPTH 0.77"
        assert "above 0.77 (the default is 0.35)" in field["title"]
        assert "applies on the next Start" in field["title"]
        assert default["text"] == "DEPTH 0.35" and "(the default)" in default["title"]
        assert absent is None and junk is None and zero is None

    def test_temperature_item_and_badge_follow_the_level(self, tmp_path):
        ok_meta, warn_meta, refuse_meta, unknown_meta, legacy_meta, none_meta = _run(tmp_path, [
            ("tempMeta", [{"temp_preflight": _temp("53~56C")}]),
            ("tempMeta", [{"temp_preflight": _temp("91~93C")}]),
            ("tempMeta", [{"temp_preflight": _temp("96C")}]),
            ("tempMeta", [{"temp_preflight": _temp(None, failure="timeout")}]),
            ("tempMeta", [{"temperature": 40.0}]),
            ("tempMeta", [{"battery": 80}]),
        ])
        assert ok_meta == {"text": "53-56 C", "cls": ""}
        assert warn_meta == {"text": "91-93 C", "cls": "warn"}
        assert refuse_meta == {"text": "96 C", "cls": "bad"}
        assert unknown_meta == {"text": "--", "cls": ""}
        assert legacy_meta == {"text": "40 C", "cls": ""}  # a local drone without the check, as before
        assert none_meta is None

        ok, warn, refuse, unknown, off, absent, junk = _run(tmp_path, [
            ("tempBadge", [{"temp_preflight": _temp("53~56C")}]),
            ("tempBadge", [{"temp_preflight": _temp("91~93C")}]),
            ("tempBadge", [{"temp_preflight": _temp("96~98C")}]),
            ("tempBadge", [{"temp_preflight": _temp(None, failure="no reply to temp? within 2.5 s")}]),
            ("tempBadge", [{"temp_preflight": _temp("100C", TempLimits(refuse_c=None))}]),
            ("tempBadge", [{"temperature": 99}]),
            ("tempBadge", [{"temp_preflight": "hot"}]),
        ])
        assert ok is None and absent is None and junk is None
        assert warn == {"text": "HOT 91-93 C", "cls": "held", "title": warn["title"]}
        assert "warn level of 90 C; a takeoff is refused at 95 C" in warn["title"]
        assert refuse["text"] == "TOO HOT 96-98 C: takeoff refused" and refuse["cls"] == "held"
        assert "refusal level of 95 C" in refuse["title"]
        assert unknown["text"] == "TEMP unknown" and "(no reply to temp? within 2.5 s)" in unknown["title"]
        assert "never holds a takeoff back" in unknown["title"]
        assert off["text"] == "HOT 100 C" and "the refusal is off" in off["title"]

    def test_the_new_badges_do_not_change_the_person_cue_badge(self, tmp_path):
        (cue,) = _run(tmp_path, [("personCueBadge", [_sensing(
            obstacle_min_safe_depth=0.77, obstacle_person_cue={"person_cue_down": True, "detection_age_s": 0.4})])])
        assert cue["text"] == "PERSON CUE down"


def test_the_card_shows_the_depth_badge_next_to_the_person_cue_and_the_temperature():
    assert "[senseBadge(d), personCueBadge(d), depthLimitBadge(d), obstacleStopBadge(d), tempBadge(d)]" in JS
    assert "const temp = tempMeta(d);" in JS
    assert "meta.appendChild(metaItem('Temp', temp.text, temp.cls));" in JS


def test_fleet_status_carries_the_depth_threshold_only_from_an_edge_that_reports_it():
    supervisor, _, _, _ = _started_supervisor()
    supervisor.coordinator.handle_message("drone-1", _telemetry("drone-1", **_sensing(obstacle_min_safe_depth=0.77)))
    supervisor.coordinator.handle_message("drone-2", _telemetry("drone-2", **_sensing(obstacle_min_safe_depth="x")))
    drones = supervisor.status_payload()["drones"]
    assert drones["drone-1"]["obstacle_min_safe_depth"] == 0.77
    assert "obstacle_min_safe_depth" not in drones["drone-2"]
    json.dumps(drones)


@pytest.mark.parametrize("status, expected", [
    ({"sensor": "forward_camera", "min_safe_depth": 0.77, "person_cue_down": False}, 0.77),
    ({"sensor": "fake"}, None),
    ({"sensor": "forward_camera", "min_safe_depth": float("nan")}, None),
    ({"sensor": "forward_camera", "min_safe_depth": 0}, None),
    (RuntimeError("status failed"), None),
])
def test_the_edge_reports_the_camera_sensors_threshold_in_telemetry(status, expected):
    from flightrisk import fleet_protocol as fp
    from tests.test_obstacle_h6_fixes import _CueSensor, _executor

    msg = _executor(_CueSensor(status)).telemetry()
    assert msg.get("obstacle_min_safe_depth") == expected
    fp.validate_edge_to_ground(msg, proto=1)
