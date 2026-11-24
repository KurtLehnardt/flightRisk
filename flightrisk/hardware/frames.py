"""Frame metadata: which camera produced a frame, where it pointed and when it was captured.

This module holds types and one signature only. It imports only the standard library at
load time. The wrapping of the edge's frame sources is added in phase A3; until then
``with_meta`` raises ``NotImplementedError`` and nothing calls it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - import only for annotations
    from flightrisk.drone.obstacle_sensor import FrameStamp
    from flightrisk.hardware.catalogue import CameraSpec


@dataclass(frozen=True)
class FrameMeta:
    """What is known about one frame beyond its pixels."""

    camera_id: str
    mount: str
    #: ``rgb`` or ``thermal``.
    sensor: str
    #: Measured for a gimbal with ``pitch_measured``, the fixed value for a fixed mount, ``None`` if unknown.
    pitch_deg: Optional[float]
    #: The decoder's stamp when it exists; ``None`` means the arrival time is the only time known.
    stamp: "Optional[FrameStamp]"
    width_px: int
    height_px: int
    #: ``rgb`` or ``bgr``, as ``obstacle_sensor``'s channel order constants.
    channel_order: str


def with_meta(source: Any, camera: "CameraSpec", pitch_reader: Callable[[str], Optional[float]]) -> Any:
    """Wrap a frame source so every frame it yields carries a :class:`FrameMeta` (phase A3)."""
    raise NotImplementedError("with_meta is wired in phase A3")


__all__ = ["FrameMeta", "with_meta"]
