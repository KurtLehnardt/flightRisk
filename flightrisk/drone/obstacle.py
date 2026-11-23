"""Vision-based obstacle avoidance using MiDaS Small depth estimation.

Uses monocular depth estimation from the drone's forward-facing camera
to detect obstacles in the flight path. MiDaS Small runs efficiently
on MPS (Apple Silicon) or CPU.

MiDaS outputs INVERSE depth — higher values mean CLOSER objects.
"""

import threading
import time

import cv2
import numpy as np
import torch

from flightrisk.drone import midas_model
from flightrisk.drone.obstacle_sensor import (
    ACTION_NO_FLOOR_REFERENCE,
    DEPTH_STATE_BLOCKED,
    DEPTH_STATE_CLEAR,
    DEPTH_STATE_NO_FLOOR_REFERENCE,
    OBSTACLE_FLOOR_RATIO_MAX,
    OBSTACLE_METRIC_FLOOR_RATIO,
    OBSTACLE_METRIC_SCORE,
    OBSTACLE_METRICS,
    valid_floor_ratio_max,
)


class ObstacleGuard:
    """Detects obstacles using MiDaS Small monocular depth estimation.

    Divides the depth map into a 3x3 grid and checks the middle row
    (left, center, right) for obstacles. If the center column's mean
    inverse depth exceeds min_safe_depth, the path is blocked.

    ``center_stat`` chooses the statistic that scores the centre region:
    ``"mean"`` (the default, what the dashboard's single-drone search uses) or
    ``"p90"``, the 90th percentile, which a thin plant stem covering only a
    small part of the region still moves. The chosen statistic drives the
    decision, the action and the confidence, and is reported as
    ``center_depth``; the result also carries ``center_p90`` and ``center_stat``.

    A depth map the guard cannot judge is reported, never passed off as a
    clear view: every result carries ``degenerate`` and ``degenerate_reason``.
    A map with a NaN or an infinite value anywhere (``"non_finite"``), a
    uniform map (``"uniform"``), a map whose 95th percentile is not positive,
    so there is no scale to normalise by (``"no_scale"``, for example all zero
    or all negative), and a map whose scores come out non-finite
    (``"non_finite_score"``) are degenerate. A degenerate result is never
    ``safe``; its ``action`` is ``"unavailable"``, so a caller that reacts to
    ``go_left``, ``go_right`` or ``reverse`` does not move, and its depths and
    confidence are 0.0. The one exception keeps its old answer: a uniform map
    of high inverse depth is still ``reverse`` with depths of 1.0, flagged
    degenerate. A valid map gives the same numbers and decision as before,
    with ``degenerate`` False and an empty reason.

    ``metric`` chooses what decides the centre. ``"score"`` (the default) is
    everything above, and its results are exactly what they were before the
    option existed. ``"floor_ratio"`` is the opt-in candidate
    (``FLIGHTRISK_OBSTACLE_METRIC``): on a map that passed every degenerate
    check, the centre is blocked when ``centre_floor_ratio`` of the raw map is
    above ``floor_ratio_max``, and when that ratio is None (no usable floor
    reference). Such a result adds ``metric``, ``floor_ratio`` (the ratio or
    None), ``floor_ratio_max`` and ``depth_state`` (``clear``, ``blocked`` or
    ``no_floor_reference``); its action is ``no_floor_reference`` for a view
    without a floor reference and is otherwise chosen from the sides as
    before. ``center_depth`` stays the score, so the score can still be
    compared. A degenerate map is handled as above under either metric.
    """

    CENTER_STATS = ("mean", "p90")
    #: ``action`` of a degenerate result.
    ACTION_UNAVAILABLE = "unavailable"
    #: ``degenerate_reason`` values.
    DEGENERATE_NON_FINITE = "non_finite"
    DEGENERATE_UNIFORM = "uniform"
    DEGENERATE_NO_SCALE = "no_scale"
    DEGENERATE_NON_FINITE_SCORE = "non_finite_score"

    def __init__(
        self,
        min_safe_depth: float = 0.35,
        check_interval: float = 0.5,
        center_stat: str = "mean",
        metric: str = OBSTACLE_METRIC_SCORE,
        floor_ratio_max: float = OBSTACLE_FLOOR_RATIO_MAX,
    ):
        if center_stat not in self.CENTER_STATS:
            raise ValueError(f"center_stat must be one of {self.CENTER_STATS}, got {center_stat!r}")
        if metric not in OBSTACLE_METRICS:
            raise ValueError(f"metric must be one of {OBSTACLE_METRICS}, got {metric!r}")
        if not valid_floor_ratio_max(floor_ratio_max):
            raise ValueError(f"floor_ratio_max must be a number from 0.5 to 1.0, got {floor_ratio_max!r}")
        self.min_safe_depth = min_safe_depth
        self.check_interval = check_interval
        self.center_stat = center_stat
        self.metric = metric
        self.floor_ratio_max = float(floor_ratio_max)

        # Select device: MPS > CPU (no CUDA on Tello laptop)
        if torch.backends.mps.is_available():
            self._device = torch.device("mps")
        else:
            self._device = torch.device("cpu")

        # MiDaS Small from the vendored code pinned to upstream commits and the
        # SHA-256 checked weights, loaded with weights_only=True (see
        # flightrisk/drone/midas_model.py). torch.hub is never used, so nothing
        # is downloaded and nothing asks on stdin; missing or altered weights
        # raise MidasUnavailable, whose message names the install step.
        self._model, self._transform = midas_model.load_midas_small()
        self._model.to(self._device)
        self._model.eval()
        #: The compute device the depth model runs on (``mps``, ``cpu``, ``cuda``). The
        #: same frame can give a slightly different map on another device, so the
        #: floor ratio's log and the replay report name it.
        self.device_name = self._device.type

        # Thread safety
        self._lock = threading.Lock()

        # Cache
        self._last_result: dict | None = None
        self._last_depth_map: np.ndarray | None = None
        self._last_check_time: float = 0.0

    def check_path(self, frame: np.ndarray) -> dict:
        """Check the flight path for obstacles.

        Args:
            frame: BGR numpy array from drone camera.

        Returns:
            Dict with keys: safe, center_depth, left_depth, right_depth,
            action, confidence, center_p90, center_stat, degenerate and
            degenerate_reason (see the class docstring).
        """
        now = time.time()

        # Return cached result if within check_interval
        if self._last_result is not None and (now - self._last_check_time) < self.check_interval:
            return self._last_result

        with self._lock:
            # Double-check cache after acquiring lock
            if self._last_result is not None and (time.time() - self._last_check_time) < self.check_interval:
                return self._last_result

            result, depth_map = self._run_inference(frame)
            self._last_result = result
            self._last_depth_map = depth_map
            self._last_check_time = time.time()
            return result

    def _run_inference(self, frame: np.ndarray) -> tuple[dict, np.ndarray]:
        """Run MiDaS inference and analyze the depth map.

        Returns:
            Tuple of (result dict, raw depth map from MiDaS).
        """
        # Convert BGR to RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Apply MiDaS transform
        input_batch = self._transform(rgb).to(self._device)

        # Inference
        with torch.no_grad():
            prediction = self._model(input_batch)

        # MiDaS outputs inverse depth (higher = closer)
        depth_map = prediction.squeeze().cpu().numpy()

        # A NaN or an infinite value anywhere (for example a half-precision
        # overflow) makes every statistic below meaningless, and NaN compares
        # false with every threshold, which would read as "nothing close".
        if not np.isfinite(depth_map).all():
            return self._degenerate(self.DEGENERATE_NON_FINITE), depth_map

        d_min = depth_map.min()
        d_max = depth_map.max()

        # Special case: uniform depth (no variation in scene). A real view
        # never gives a uniform map, so it is degenerate whatever its level.
        if d_max - d_min < 1e-6:
            if d_max > 1.0:
                # High uniform inverse depth = everything close (flat wall).
                # It already stops the drone, so it keeps its old answer.
                result = {
                    "safe": False, "center_depth": 1.0, "left_depth": 1.0,
                    "right_depth": 1.0, "action": "reverse", "confidence": 0.5,
                    "center_p90": 1.0, "center_stat": self.center_stat,
                    "degenerate": True, "degenerate_reason": self.DEGENERATE_UNIFORM,
                }
                return result, depth_map
            return self._degenerate(self.DEGENERATE_UNIFORM), depth_map

        # Use 95th percentile as reference scale (avoids outlier sensitivity).
        # Without a positive scale (all zero, all negative) the map cannot be
        # normalised and says nothing about what is close.
        p95 = np.percentile(depth_map, 95)
        if not p95 > 1e-6:
            return self._degenerate(self.DEGENERATE_NO_SCALE), depth_map
        # A huge value over a tiny scale can overflow; the scores are checked below.
        with np.errstate(over="ignore", invalid="ignore"):
            depth_norm = depth_map / p95

            # Divide into 3x3 grid, focus on middle row
            h, w = depth_norm.shape
            row_start = h // 3
            row_end = 2 * h // 3
            col_third = w // 3

            middle_row = depth_norm[row_start:row_end, :]
            left_region = middle_row[:, :col_third]
            center_region = middle_row[:, col_third:2 * col_third]
            right_region = middle_row[:, 2 * col_third:]

            left_depth = float(np.mean(left_region))
            center_mean = float(np.mean(center_region))
            center_p90 = float(np.percentile(center_region, 90))
            center_depth = center_p90 if self.center_stat == "p90" else center_mean
            right_depth = float(np.mean(right_region))
        if not all(np.isfinite(v) for v in (left_depth, center_mean, center_p90, right_depth)):
            return self._degenerate(self.DEGENERATE_NON_FINITE_SCORE), depth_map

        if self.metric == OBSTACLE_METRIC_FLOOR_RATIO:
            return self._floor_ratio_result(depth_map, center_depth, left_depth, right_depth, center_p90), depth_map

        # Determine if center is blocked
        center_blocked = center_depth > self.min_safe_depth
        left_blocked = left_depth > self.min_safe_depth
        right_blocked = right_depth > self.min_safe_depth

        # Compute action
        if not center_blocked:
            action = "clear"
            safe = True
        elif left_blocked and right_blocked:
            action = "reverse"
            safe = False
        elif left_depth < right_depth:
            # Left is clearer (lower inverse depth = farther away)
            action = "go_left"
            safe = False
        else:
            action = "go_right"
            safe = False

        # Confidence: how certain we are about the assessment
        # Higher when the depth difference between blocked/clear paths is large
        if safe:
            confidence = min(1.0, (self.min_safe_depth - center_depth) / self.min_safe_depth)
        else:
            all_depths = [left_depth, center_depth, right_depth]
            spread = max(all_depths) - min(all_depths)
            confidence = min(1.0, spread / self.min_safe_depth) if self.min_safe_depth > 0 else 0.5

        confidence = max(0.0, confidence)

        return {
            "safe": safe,
            "center_depth": center_depth,
            "left_depth": left_depth,
            "right_depth": right_depth,
            "action": action,
            "confidence": round(confidence, 3),
            "center_p90": center_p90,
            "center_stat": self.center_stat,
            "degenerate": False,
            "degenerate_reason": "",
        }, depth_map

    def _floor_ratio_result(
        self, depth_map: np.ndarray, center_depth: float, left_depth: float, right_depth: float, center_p90: float,
    ) -> dict:
        """The result of the opt-in ``floor_ratio`` metric for a map that passed every degenerate check.

        The ratio is ``centre_floor_ratio`` of the raw map, the same call on the
        same map that ``scripts/obstacle_replay.py --metrics-report`` makes, so a
        threshold fitted there means the same here. None (no usable floor
        reference) is blocked and never clear. The sides are judged as before,
        against ``min_safe_depth``, only to choose the action of a blocked view.
        """
        ratio = centre_floor_ratio(depth_map)
        limit = self.floor_ratio_max
        if ratio is None:
            safe, action, state, confidence = False, ACTION_NO_FLOOR_REFERENCE, DEPTH_STATE_NO_FLOOR_REFERENCE, 0.0
        elif ratio <= limit:
            safe, action, state = True, "clear", DEPTH_STATE_CLEAR
            confidence = min(1.0, (limit - ratio) / limit)
        else:
            safe, state = False, DEPTH_STATE_BLOCKED
            confidence = min(1.0, (ratio - limit) / limit)
            left_blocked = left_depth > self.min_safe_depth
            right_blocked = right_depth > self.min_safe_depth
            if left_blocked and right_blocked:
                action = "reverse"
            elif left_depth < right_depth:
                action = "go_left"
            else:
                action = "go_right"
        return {
            "safe": safe,
            "center_depth": center_depth,
            "left_depth": left_depth,
            "right_depth": right_depth,
            "action": action,
            "confidence": round(max(0.0, confidence), 3),
            "center_p90": center_p90,
            "center_stat": self.center_stat,
            "degenerate": False,
            "degenerate_reason": "",
            "metric": OBSTACLE_METRIC_FLOOR_RATIO,
            "floor_ratio": ratio,
            "floor_ratio_max": limit,
            "depth_state": state,
        }

    def _degenerate(self, reason: str) -> dict:
        """The result for a depth map that cannot be judged: never safe, and no move suggested."""
        return {
            "safe": False, "center_depth": 0.0, "left_depth": 0.0,
            "right_depth": 0.0, "action": self.ACTION_UNAVAILABLE, "confidence": 0.0,
            "center_p90": 0.0, "center_stat": self.center_stat,
            "degenerate": True, "degenerate_reason": reason,
        }

    def get_depth_visualization(self, frame: np.ndarray) -> np.ndarray:
        """Return a colorized depth map with safety overlays.

        Args:
            frame: BGR numpy array from drone camera.

        Returns:
            Colorized depth map with green/red region overlays.
        """
        result = self.check_path(frame)

        with self._lock:
            depth_map = self._last_depth_map

        if depth_map is None:
            return frame.copy()

        # Normalize to 0-255 for visualization
        d_min = depth_map.min()
        d_max = depth_map.max()
        if d_max - d_min > 1e-6:
            depth_vis = ((depth_map - d_min) / (d_max - d_min) * 255).astype(np.uint8)
        else:
            depth_vis = np.zeros_like(depth_map, dtype=np.uint8)

        colored = cv2.applyColorMap(depth_vis, cv2.COLORMAP_MAGMA)

        h_orig, w_orig = frame.shape[:2]
        colored = cv2.resize(colored, (w_orig, h_orig))

        h, w = colored.shape[:2]
        col_third = w // 3
        row_start = h // 3
        row_end = 2 * h // 3

        regions = [
            (0, col_third, result["left_depth"]),
            (col_third, 2 * col_third, result["center_depth"]),
            (2 * col_third, w, result["right_depth"]),
        ]

        overlay = colored.copy()
        for x1, x2, depth in regions:
            color = (0, 0, 255) if depth > self.min_safe_depth else (0, 255, 0)
            cv2.rectangle(overlay, (x1, row_start), (x2, row_end), color, 3)

        cv2.addWeighted(overlay, 0.7, colored, 0.3, 0, colored)
        return colored


# ---------------------------------------------------------------------------
# Candidate obstacle metrics.
#
# These are pure functions of a raw MiDaS inverse-depth map (higher = closer).
# scripts/obstacle_replay.py --metrics-report compares them on recorded
# flights. By default the flight path calls none of them: the decision uses the
# p95 normalised centre score computed in ObstacleGuard. Only an ObstacleGuard
# built with metric="floor_ratio" (the opt-in FLIGHTRISK_OBSTACLE_METRIC)
# decides with centre_floor_ratio; that threshold is not validated.
# ---------------------------------------------------------------------------

#: Rows of the floor band: the bottom fifth of the map.
FLOOR_BAND_ROW_FRACTION = 0.20
#: The floor band and the top band use the middle 60 percent of the columns.
FLOOR_BAND_COL_MARGIN = 0.20
#: The top band, used only to check that the floor looks nearer than the top of the view.
TOP_BAND_ROW_FRACTION = 0.20
#: A floor median at or below this is treated as no floor signal.
_MIN_FLOOR_DEPTH = 1e-6


def _centre_region(depth_map: np.ndarray) -> np.ndarray:
    """The middle row, centre third of the map: the region the current score reads."""
    h, w = depth_map.shape
    col_third = w // 3
    return depth_map[h // 3:2 * h // 3, col_third:2 * col_third]


def _usable_map(depth_map) -> np.ndarray | None:
    """The map as a 2-D float array, or None when it is not a finite 2-D map of at least 3x3."""
    try:
        arr = np.asarray(depth_map, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if arr.ndim != 2 or arr.shape[0] < 3 or arr.shape[1] < 3:
        return None
    if not np.isfinite(arr).all():
        return None
    return arr


def centre_floor_ratio(depth_map) -> float | None:
    """The centre's 90th percentile divided by the median of the floor band.

    The centre is the middle row, centre third (the region the current score
    reads), taken from the raw map. The floor band is the bottom 20 percent of
    the rows over the middle 60 percent of the columns. The band does not see
    the ground just below the drone: it sees the ground roughly 1.9 to 3.1
    times the camera height ahead of it. So the ratio is only a yardstick at a
    known camera height, and a threshold fitted at one height (the slot's 100
    or 130 cm) does not carry over to another; record at flight altitude. A
    ratio near 1 means the centre is about as close as that stretch of ground,
    and a larger ratio means closer still. Unlike the p95 normalisation it does
    not depend on whatever happens to be the closest thing in the frame. MiDaS
    gives relative inverse depth with an unknown offset and scale per frame, so
    a plain ratio changes when the offset does; see ``centre_floor_span``.

    Returns None when the ratio is not usable:
      * the map is not a finite 2-D array of at least 3x3,
      * the floor median is zero, negative or too small to divide by,
      * the floor looks farther than the top band of the view (its median is
        below the top band's), which means the bottom of the frame is not a
        nearby floor.
    """
    arr = _usable_map(depth_map)
    if arr is None:
        return None
    h, w = arr.shape
    col_lo = int(w * FLOOR_BAND_COL_MARGIN)
    col_hi = w - col_lo
    floor_rows = max(1, int(round(h * FLOOR_BAND_ROW_FRACTION)))
    top_rows = max(1, int(round(h * TOP_BAND_ROW_FRACTION)))
    floor = float(np.median(arr[h - floor_rows:, col_lo:col_hi]))
    top = float(np.median(arr[:top_rows, col_lo:col_hi]))
    if not np.isfinite(floor) or not floor > _MIN_FLOOR_DEPTH:
        return None
    if floor < top:
        return None
    centre_p90 = float(np.percentile(_centre_region(arr), 90))
    ratio = centre_p90 / floor
    return ratio if np.isfinite(ratio) else None


def centre_floor_span(depth_map) -> float | None:
    """The centre's rise over the top band, as a share of the floor's rise over the top band.

    ``(centre_p90 - top_median) / (floor_median - top_median)`` with the same centre, floor
    band and top band as ``centre_floor_ratio``. Adding the same offset to every pixel, or
    scaling every pixel, leaves it unchanged, which MiDaS's per-frame offset and scale break
    for a plain ratio. About 1 means the centre is as close as the floor band; more is closer.

    Returns None when the map is not a finite 2-D array of at least 3x3, when the floor band
    is not nearer than the top band, or when their difference is too close to zero to divide
    by (below 0.1 percent of the map's range).
    """
    arr = _usable_map(depth_map)
    if arr is None:
        return None
    h, w = arr.shape
    col_lo = int(w * FLOOR_BAND_COL_MARGIN)
    col_hi = w - col_lo
    floor_rows = max(1, int(round(h * FLOOR_BAND_ROW_FRACTION)))
    top_rows = max(1, int(round(h * TOP_BAND_ROW_FRACTION)))
    floor = float(np.median(arr[h - floor_rows:, col_lo:col_hi]))
    top = float(np.median(arr[:top_rows, col_lo:col_hi]))
    denominator = floor - top
    spread = float(arr.max() - arr.min())
    if not denominator > max(_MIN_FLOOR_DEPTH, 1e-3 * spread):
        return None
    span = (float(np.percentile(_centre_region(arr), 90)) - top) / denominator
    return span if np.isfinite(span) else None


def centre_sides_ratio(depth_map) -> float | None:
    """The centre's 90th percentile divided by the mean of the two side thirds' 90th percentiles.

    All three regions are in the middle row. A value above 1 means the centre
    is closer than the sides. Returns None for an unusable map or when the
    sides have no positive depth to divide by.
    """
    arr = _usable_map(depth_map)
    if arr is None:
        return None
    h, w = arr.shape
    col_third = w // 3
    middle = arr[h // 3:2 * h // 3, :]
    left = float(np.percentile(middle[:, :col_third], 90))
    right = float(np.percentile(middle[:, 2 * col_third:], 90))
    centre = float(np.percentile(middle[:, col_third:2 * col_third], 90))
    sides = (left + right) / 2.0
    if not np.isfinite(sides) or not sides > _MIN_FLOOR_DEPTH:
        return None
    ratio = centre / sides
    return ratio if np.isfinite(ratio) else None
