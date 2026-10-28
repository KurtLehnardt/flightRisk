"""Docs and launcher fixes from the PR #87 final review (R-112)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def _doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_placement_step_present():
    for name in ("demo-script.md", "fleet-setup.md"):
        text = _doc(name)
        assert "near-left corner" in text, name
        assert "250 cm to its right" in text, name
        assert "4 m depth" in text, name
        assert "behind the takeoff line" in text, name


def test_presenter_outside_flight_path():
    text = _doc("demo-script.md")
    assert "step into a drone's camera view" not in text
    assert "at least 1.5 m beyond the far edge" in text
    assert "never in a flight path" in text
    assert "drop edge-2's Wi-Fi" in text


def test_launchers_export_geofence_and_appearance_only():
    sh = (ROOT / "scripts" / "demo_edge.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "scripts" / "demo_edge.ps1").read_text(encoding="utf-8")
    assert 'FLIGHTRISK_GEOFENCE_CM="${FLIGHTRISK_GEOFENCE_CM:-450}"' in sh
    assert "FLIGHTRISK_APPEARANCE_ONLY=1" in sh
    assert re.search(r'-not \$env:FLIGHTRISK_GEOFENCE_CM\).*"450"', ps1)
    assert '$env:FLIGHTRISK_APPEARANCE_ONLY = "1"' in ps1


def test_privacy_docs_match_edge_behaviour():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name, text in (
        ("README.md", readme),
        ("demo-script.md", _doc("demo-script.md")),
        ("privacy-design.md", _doc("privacy-design.md")),
        ("pitch.md", _doc("pitch.md")),
    ):
        assert re.search(r"person crops", text, re.IGNORECASE), name
        assert "token-authenticated" in text, name
    assert "--with-face" in readme and "--with-face" in _doc("privacy-design.md")
    assert "never sent over the network by either the dashboard or the mobile app" not in _doc("privacy-design.md")


def test_rung2_stops_edge_2_first():
    text = _doc("demo-script.md")
    rung = text[text.index("2. **One Tello live and one fake edge.**"):text.index("3. **Recorded video")]
    assert rung.index("stop the real edge") < rung.index("--source fake")


def test_person_size_assumption_labelled():
    pitch = _doc("pitch.md")
    assert pitch.count("1.7 m") >= 2
    for m in re.finditer(r"1\.7 m", pitch):
        window = pitch[max(0, m.start() - 200): m.end() + 200]
        assert "ASSUMPTION" in window


def test_privacy_design_current_state():
    text = _doc("privacy-design.md")
    # The pre-hackathon "today / planned" framing and the stale always-constructs claim are gone.
    assert "`_init_pipeline` always constructs `FaceRecognizer`" not in text
    assert "798f21a" not in text
    assert "Today:" not in text and "Planned (" not in text
    # The face embedding broadcast to edges is stated, with its default-demo exemption.
    assert "broadcast_target" in text
    assert "not appearance-only" in text
    assert "does not happen in the default demo configuration" in text
    assert "Face embeddings are never sent over the network by the dashboard" not in text
    # The edge link is unencrypted ws:// protected by a shared token.
    assert "plain `ws://`" in text
    assert "FLIGHTRISK_EDGE_TOKEN" in text
    assert "private network" in text


def test_demo_script_rung2_and_geofence_450():
    text = _doc("demo-script.md")
    rung = text[text.index("2. **One Tello live and one fake edge.**"):text.index("3. **Recorded video")]
    assert "refuses a second live edge" in rung
    assert "swap routing" not in rung
    assert "450 cm geofence around its takeoff point by default" in text
    assert "300 cm geofence around its takeoff point by default" not in text
    assert "500 cm geofence around its takeoff point by default" not in text


def test_demo_edge_ps1_restores_env():
    ps1 = (ROOT / "scripts" / "demo_edge.ps1").read_text(encoding="utf-8")
    assert "$savedGeofence = $env:FLIGHTRISK_GEOFENCE_CM" in ps1
    assert "$savedAppearanceOnly = $env:FLIGHTRISK_APPEARANCE_ONLY" in ps1
    body = ps1[ps1.index("try {"):]
    assert "finally {" in body
    set_at = body.index('$env:FLIGHTRISK_APPEARANCE_ONLY = "1"')
    finally_at = body.index("finally {")
    assert set_at < finally_at
    restore = body[finally_at:]
    assert "$env:FLIGHTRISK_GEOFENCE_CM = $savedGeofence" in restore
    assert "$env:FLIGHTRISK_APPEARANCE_ONLY = $savedAppearanceOnly" in restore
    # The exit code is returned after the restore, not from inside the try block.
    assert body.rindex("exit $exitCode") > finally_at


def test_demo_script_hotkey_in_text_fields_and_geofence_launchers():
    """R-116: the LAND ALL hotkey inside text fields, and where the 450 cm geofence comes from."""
    text = _doc("demo-script.md")
    core = text[text.index("- 2:15 Say:"):]
    core = core[:core.index("\n")]
    assert "Ctrl/Cmd+Shift+L" in core and "text field" in core
    limits = text[text.index("Each edge enforces its own limits"):]
    limits = limits[:limits.index("\n")]
    assert "Ctrl/Cmd+Shift+L" in limits and "text field" in limits
    assert "450 cm geofence around its takeoff point by default, as set by the demo launchers" in limits
    assert "the code default is 500 cm" in limits
