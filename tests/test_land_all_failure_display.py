"""A failed LAND ALL must never read as "LAND ALL sent" on the dashboard.

The template-contract checks read static/ui/ui.js as text. The Node checks run the real feedback helpers
(the block between the land-all-feedback markers) with stub inputs. LAND ALL itself, Shift+L and every landing
path are checked to stay present and ungated: only the feedback text changed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard"
JS = (DASH / "static" / "ui" / "ui.js").read_text()
PAGE = (DASH / "templates" / "index.html").read_text()

FAILED = (
    "LAND ALL FAILED: no drone was reached. Land each drone by hand "
    "(per-drone Land, or the drone's own controls)."
)


def _block() -> str:
    start = JS.index("// BEGIN land-all-feedback")
    return JS[start : JS.index("// END land-all-feedback")]


def test_failure_text_is_in_the_page_script():
    block = _block()
    assert "LAND ALL FAILED: no drone was reached. Land each drone by hand " in block
    assert "(per-drone Land, or the drone\\'s own controls)." in block


def test_fleet_event_land_all_branch_checks_for_failure_before_saying_sent():
    branch = JS[JS.index("else if (kind === 'land_all') {"):]
    branch = branch[: branch.index("else if (kind === 'profile_switch_pending')")]
    assert "landAllEventFeedback(ev)" in branch
    assert branch.index("landAllEventFeedback(ev)") < branch.index("'LAND ALL sent'")


def test_land_all_ack_is_handled():
    assert "socket.on('land_all_ack'" in JS


def test_land_all_button_and_shift_l_stay_present_and_ungated():
    assert 'id="land-all"' in PAGE
    assert "disabled" not in re.search(r"<[^>]*id=\"land-all\"[^>]*>", PAGE).group(0)
    assert JS.count("socket.emit('land_all');") == 1
    fn = JS[JS.index("function landAll() {"):]
    fn = fn[: fn.index("on('land-all', 'click', landAll);")]
    assert fn.split("{", 1)[1].lstrip().startswith("socket.emit('land_all');")
    assert "showFleetBanner('LAND ALL sent');" in fn
    assert not re.search(r"\bif\b|return", fn)
    # Nothing ever disables the button.
    assert not re.search(r"land-all['\"]\)\.disabled|\$\('land-all'\)\.disabled|landAllBtn\.disabled", JS)
    handler = JS[JS.index("document.addEventListener('keydown', function (e) {"):]
    handler = handler[: handler.index("}, true);")]
    assert "landAll();" in handler


NODE = shutil.which("node")
HARNESS = r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const cases = JSON.parse(process.argv[3]);
const f = new Function(src + '; return {landAllAckFeedback, landAllEventFeedback};')();
console.log(JSON.stringify(cases.map((c) => (c.kind === 'ack' ? f.landAllAckFeedback(c.p) : f.landAllEventFeedback(c.p)))));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
class TestFeedbackHelpers:
    @staticmethod
    def run(tmp_path, cases):
        f = tmp_path / "block.js"
        f.write_text(_block())
        harness = tmp_path / "h.js"
        harness.write_text(HARNESS)
        res = subprocess.run([NODE, str(harness), str(f), json.dumps(cases)], capture_output=True, text=True, check=True)
        return json.loads(res.stdout)

    def test_failed_event_shows_failure_text_not_sent(self, tmp_path):
        (r,) = self.run(tmp_path, [{"kind": "event", "p": {"event": "land_all", "detail": "failed: drone-2"}}])
        assert r["failed"] is True
        assert r["text"] == FAILED + " failed: drone-2"
        assert "sent" not in r["text"].lower()

    def test_successful_event_is_not_a_failure(self, tmp_path):
        (r,) = self.run(tmp_path, [{"kind": "event", "p": {"event": "land_all", "detail": "sent to all"}}])
        assert r is None

    @pytest.mark.parametrize("ack", [
        {"coordinator": None, "local": None, "reached_any": False, "transport": None},
        {"coordinator": None, "local": None, "transport": None},
        {"coordinator": 2, "local": None, "reached_any": None, "transport": 2},
    ])
    def test_ack_not_reached_shows_failure(self, tmp_path, ack):
        (r,) = self.run(tmp_path, [{"kind": "ack", "p": ack}])
        assert r == {"failed": True, "text": FAILED}

    def test_ack_reached_shows_success(self, tmp_path):
        a, b = self.run(tmp_path, [
            {"kind": "ack", "p": {"coordinator": 2, "local": None, "reached_any": True, "transport": 2}},
            {"kind": "ack", "p": {"coordinator": None, "local": 1, "reached_any": True, "transport": None}},
        ])
        assert a is None and b is None

    def test_ack_partial_names_reached_and_unreached_paths(self, tmp_path):
        (r,) = self.run(tmp_path, [{"kind": "ack", "p": {"coordinator": 0, "local": None, "reached_any": True, "transport": 2}}])
        assert r["failed"] is False
        assert "Reached: transport 2" in r["text"] and "Reached none: coordinator" in r["text"]
