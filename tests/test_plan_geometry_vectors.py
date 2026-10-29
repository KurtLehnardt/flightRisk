"""The Python and browser geometry agree on shared test vectors.

``tests/fixtures/plan_geometry_vectors.json`` holds anchor conversions and
drawn paths with their expected lanes, traces and geofence results. The Python
modules (``flightrisk/drone/geo.py``, ``flightrisk/drone/plan_paths.py``) and
the browser module (``flightrisk/dashboard/static/ui/plan_geom.js``, run under
Node when it is installed) must both reproduce them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from flightrisk.drone import plan_paths as pp
from flightrisk.drone.geo import PlanAnchor, frame_to_latlon, latlon_to_frame

ROOT = Path(__file__).resolve().parent.parent
VECTORS = json.loads((ROOT / "tests" / "fixtures" / "plan_geometry_vectors.json").read_text())
GEOM_JS = ROOT / "flightrisk" / "dashboard" / "static" / "ui" / "plan_geom.js"


def test_python_reproduces_the_frame_vectors():
    for case in VECTORS["frame"]:
        anchor = PlanAnchor.from_dict(case["anchor"])
        lat, lon = frame_to_latlon(anchor, case["x_cm"], case["y_cm"])
        assert lat == pytest.approx(case["lat"], abs=1e-12)
        assert lon == pytest.approx(case["lon"], abs=1e-12)
        x, y = latlon_to_frame(anchor, case["lat"], case["lon"])
        assert (x, y) == (pytest.approx(case["x_cm"], abs=1e-6), pytest.approx(case["y_cm"], abs=1e-6))


def test_python_reproduces_the_path_vectors():
    for case in VECTORS["paths"]:
        converted = pp.path_to_lanes(case["vertices"], fence_cm=case["fence_cm"])
        assert [dict(lane) for lane in converted.lanes] == case["lanes"]
        assert [list(p) for p in converted.trace] == case["trace"]
        assert list(converted.trace_segment) == case["trace_segment"]
        assert converted.max_deviation_cm == pytest.approx(case["max_deviation_cm"], abs=1e-9)
        if case["fence_cm"] is not None:
            outside = pp.geofence_violation(converted, case["fence_cm"])
            expected = case["geofence"]
            assert (None if outside is None else {"vertex": outside[0], "point": list(outside[1])}) == expected


_NODE_SCRIPT = r"""
const geom = require(process.argv[1]);
const vectors = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = { frame: [], paths: [] };
vectors.frame.forEach((c) => {
    const ll = geom.frameToLatLon(c.anchor, c.x_cm, c.y_cm);
    const back = geom.latLonToFrame(c.anchor, c.lat, c.lon);
    out.frame.push({ lat: ll[0], lon: ll[1], x_cm: back[0], y_cm: back[1] });
});
vectors.paths.forEach((c) => {
    const conv = geom.pathToLanes(c.vertices, c.fence_cm === null ? undefined : c.fence_cm);
    const fence = c.fence_cm === null ? null : geom.geofenceViolation(conv, c.fence_cm);
    out.paths.push({ lanes: conv.lanes, trace: conv.trace, trace_segment: conv.traceSegment,
                     max_deviation_cm: conv.maxDeviationCm, geofence: fence });
});
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_browser_geometry_reproduces_the_vectors():
    result = subprocess.run(
        ["node", "-e", _NODE_SCRIPT, str(GEOM_JS)],
        input=json.dumps(VECTORS), capture_output=True, text=True, timeout=60, check=True,
    )
    out = json.loads(result.stdout)
    for case, got in zip(VECTORS["frame"], out["frame"], strict=True):
        assert got["lat"] == pytest.approx(case["lat"], abs=1e-12)
        assert got["lon"] == pytest.approx(case["lon"], abs=1e-12)
        assert got["x_cm"] == pytest.approx(case["x_cm"], abs=1e-6)
        assert got["y_cm"] == pytest.approx(case["y_cm"], abs=1e-6)
    for case, got in zip(VECTORS["paths"], out["paths"], strict=True):
        assert got["lanes"] == case["lanes"], case["vertices"]
        assert got["trace"] == case["trace"], case["vertices"]
        assert got["trace_segment"] == case["trace_segment"], case["vertices"]
        assert got["max_deviation_cm"] == pytest.approx(case["max_deviation_cm"], abs=1e-9)
        assert got["geofence"] == case["geofence"], case["vertices"]


def test_the_vectors_cover_the_interesting_cases():
    paths = VECTORS["paths"]
    assert any(case["geofence"] for case in paths), "a geofence refusal"
    assert any(any(lane["distance_cm"] < 40 for lane in case["lanes"]) for case in paths), "an overshoot pair"
    assert any(len(case["lanes"]) > 10 for case in paths), "a staircase"
    assert any(any(isinstance(v, float) for vertex in case["vertices"] for v in vertex) for case in paths), "rounding"
    assert {case["anchor"]["heading_deg"] for case in VECTORS["frame"]} >= {0.0, 270.0}
