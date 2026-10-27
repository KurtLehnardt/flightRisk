"""Static checks for the Windows edge setup script and document (R-103)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_windows_setup_doc_and_script():
    script = (ROOT / "scripts" / "install_windows.ps1").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "windows-edge-setup.md").read_text(encoding="utf-8")

    assert "py -3.13" in script
    assert "venv\\Scripts\\python.exe" in script
    assert "-m pip install -r requirements.txt -r requirements-dev.txt" in script
    assert "yolo11n.pt" in script
    assert "ollama pull" not in script.lower()
    assert "convert_insightface" not in script

    for needle in (
        "py -3.13",
        "venv\\Scripts\\python.exe",
        "New-NetIPAddress",
        "New-NetFirewallRule",
        "FLIGHTRISK_EDGE_TOKEN",
        "demo_edge.ps1",
        "check_fleet_link.py",
    ):
        assert needle in doc, needle
