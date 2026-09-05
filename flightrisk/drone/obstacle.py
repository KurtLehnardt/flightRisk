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


class ObstacleGuard:
    """Detects obstacles using MiDaS Small monocular depth estimation.

    Divides the depth map into a 3x3 grid and checks the middle row
    (left, center, right) for obstacles. If the center column's mean
    inverse depth exceeds min_safe_depth, the path is blocked.
    """

    def __init__(self, min_safe_depth: float = 0.35, check_interval: float = 0.5):
        self.min_safe_depth = min_safe_depth
        self.check_interval = check_interval

        # Select device: MPS > CPU (no CUDA on Tello laptop)
        if torch.backends.mps.is_available():
            self._device = torch.device("mps")
        else:
            self._device = torch.device("cpu")

        # Load MiDaS Small model
        self._model = torch.hub.load("intel-isl/MiDaS", "MiDaS_small")
        self._model.to(self._device)
        self._model.eval()

        # Load transforms
        midas_transforms = torch.hub.load("intel-isl/MiDaS", "transforms")
        self._transform = midas_transforms.small_transform

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
            action, confidence.
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

        d_min = depth_map.min()
        d_max = depth_map.max()

        # Special case: uniform depth (no variation in scene)
        if d_max - d_min < 1e-6:
            if d_max < 1e-6:
                result = {
                    "safe": True, "center_depth": 0.0, "left_depth": 0.0,
                    "right_depth": 0.0, "action": "clear", "confidence": 1.0,
                }
            elif d_max > 1.0:
                # High uniform inverse depth = everything close (flat wall)
                result = {
                    "safe": False, "center_depth": 1.0, "left_depth": 1.0,
                    "right_depth": 1.0, "action": "reverse", "confidence": 0.5,
                }
            else:
                # Low uniform inverse depth = everything far away
                result = {
                    "safe": True, "center_depth": 0.0, "left_depth": 0.0,
                    "right_depth": 0.0, "action": "clear", "confidence": 0.8,
                }
            return result, depth_map

        # Use 95th percentile as reference scale (avoids outlier sensitivity)
        p95 = np.percentile(depth_map, 95)
        if p95 > 1e-6:
            depth_norm = depth_map / p95
        else:
            # Near-zero output everywhere — nothing close
            depth_norm = np.zeros_like(depth_map)

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
        center_depth = float(np.mean(center_region))
        right_depth = float(np.mean(right_region))

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
        }, depth_map

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
