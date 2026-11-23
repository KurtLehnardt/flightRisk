"""Shared image downscaling for images that leave the frame loop.

Used by the edge crop validator (``flightrisk.dashboard.alerts``) and the
Claude upload path (``flightrisk.reasoning.claude_agent``) so both shrink
images the same way.
"""

import cv2
import numpy as np


def downscale_long_edge(image: np.ndarray, max_edge: int) -> np.ndarray:
    """Shrink ``image`` so its long edge is at most ``max_edge`` pixels.

    Keeps the aspect ratio, uses area interpolation, and never upscales: an
    image already within the limit is returned unchanged (the same object).
    """
    h, w = image.shape[:2]
    long_edge = max(h, w)
    if long_edge <= max_edge:
        return image
    scale = max_edge / long_edge
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
