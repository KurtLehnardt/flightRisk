#!/usr/bin/env python3
"""Make the synthetic test picture a user's own person detector must find a person in.

The iOS app checks a detector the user imports (Settings > Person Detector) by running it on
this picture: it must find at least one person (class 0) scoring 0.25 or more. The picture is
drawn, not photographed: thirteen person-like figures, 24 to 120 pixels tall, on a grass-like
texture, made by the same procedure as the golden fixtures of the model pipeline
(``scripts/mobile_models/make_models.py``, ``silhouette_scene``), so it holds no real person.

Run it from the repository root with a Python that has numpy and OpenCV (FlightRisk's venv, or
the model conversion venv):

    venv/bin/python ios/scripts/make_person_check_scene.py

The seed was chosen so that YOLOX-S finds seven people (best 0.73) and a YOLO11n export finds
nine at 0.25 or more (best 0.64); see docs/mobile-models.md, "Your own person detector (iOS)".
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "mobile_models"))

SEED = 20261205
SIZE = (640, 640)
HEIGHTS = (24, 32, 48, 64, 80, 96, 120, 24, 32, 48, 64, 80, 96)
OUT = ROOT / "ios" / "FlightRisk" / "Resources" / "person-check-scene.png"


def main() -> int:
    import cv2

    import make_models

    frame, _ = make_models.silhouette_scene(seed=SEED, size=SIZE, heights=HEIGHTS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(OUT), frame, [cv2.IMWRITE_PNG_COMPRESSION, 9]):
        print(f"could not write {OUT}", file=sys.stderr)
        return 1
    print(OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
