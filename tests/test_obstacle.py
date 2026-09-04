"""Tests for vision-based obstacle avoidance (ObstacleGuard).

All tests mock torch.hub.load so no model download is required.
"""

import threading
import time

import numpy as np
import pytest
import torch


# ---------------------------------------------------------------------------
# Fixtures — mock MiDaS model and transforms
# ---------------------------------------------------------------------------

class MockModel(torch.nn.Module):
    """Mock MiDaS model that returns a configurable depth map."""

    def __init__(self):
        super().__init__()
        # Default: uniform depth (safe scene)
        self._depth_map = torch.ones(1, 256, 256) * 0.1

    def set_depth_map(self, depth_map: torch.Tensor):
        self._depth_map = depth_map

    def forward(self, x):
        return self._depth_map.clone()


class MockTransform:
    def __call__(self, img):
        return torch.zeros(1, 3, 256, 256)


class MockTransforms:
    small_transform = MockTransform()


# Shared mock model so tests can manipulate its output
_mock_model = MockModel()


def _mock_hub_load(repo, model_name, **kwargs):
    if model_name == "MiDaS_small":
        return _mock_model
    elif model_name == "transforms":
        return MockTransforms()
    raise ValueError(f"Unknown model: {model_name}")


@pytest.fixture(autouse=True)
def mock_midas(monkeypatch):
    """Patch torch.hub.load globally for all tests."""
    monkeypatch.setattr("torch.hub.load", _mock_hub_load)
    # Reset mock model to default uniform depth
    _mock_model.set_depth_map(torch.ones(1, 256, 256) * 0.1)
    yield _mock_model


@pytest.fixture
def guard():
    from flightrisk.drone.obstacle import ObstacleGuard
    return ObstacleGuard(min_safe_depth=0.35, check_interval=0.0)


@pytest.fixture
def frame():
    """A dummy 720p BGR frame."""
    return np.zeros((720, 960, 3), dtype=np.uint8)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInitialization:
    def test_model_loads(self, guard):
        """ObstacleGuard should initialize without error."""
        assert guard is not None
        assert guard._model is not None
        assert guard._transform is not None

    def test_device_selection(self, guard):
        """Device should be mps or cpu."""
        assert guard._device.type in ("mps", "cpu")


class TestCheckPathStructure:
    def test_returns_correct_keys(self, guard, frame):
        """check_path must return all required keys."""
        result = guard.check_path(frame)
        required_keys = {"safe", "center_depth", "left_depth", "right_depth", "action", "confidence"}
        assert required_keys == set(result.keys())

    def test_depth_values_normalized(self, guard, frame):
        """All depth values should be in 0-1 range."""
        result = guard.check_path(frame)
        for key in ("center_depth", "left_depth", "right_depth"):
            assert 0.0 <= result[key] <= 1.0, f"{key}={result[key]} not in [0,1]"

    def test_confidence_normalized(self, guard, frame):
        """Confidence should be in 0-1 range."""
        result = guard.check_path(frame)
        assert 0.0 <= result["confidence"] <= 1.0


class TestUniformDepth:
    def test_uniform_low_depth_is_safe(self, mock_midas, guard, frame):
        """Uniform low inverse depth (everything far away) should be safe."""
        mock_midas.set_depth_map(torch.ones(1, 256, 256) * 0.1)
        result = guard.check_path(frame)
        assert result["safe"] is True
        assert result["action"] == "clear"

    def test_all_black_frame_is_safe(self, mock_midas, guard, frame):
        """All-black frame with uniform depth should report safe."""
        mock_midas.set_depth_map(torch.zeros(1, 256, 256))
        result = guard.check_path(frame)
        # Uniform zero depth normalizes to all-zero — below threshold
        assert result["safe"] is True
        assert result["action"] == "clear"

    def test_uniform_high_depth_is_blocked(self, mock_midas, guard, frame):
        """Uniform high inverse depth (flat wall approaching) must NOT be safe."""
        mock_midas.set_depth_map(torch.ones(1, 256, 256) * 5.0)
        result = guard.check_path(frame)
        assert result["safe"] is False
        assert result["action"] == "reverse"


class TestObstacleCenter:
    def test_center_obstacle_blocks(self, mock_midas, guard, frame):
        """High inverse depth in center column should block the path."""
        depth = torch.ones(1, 256, 256) * 0.1
        # Center third of middle row: rows 85-170, cols 85-170
        depth[0, 85:170, 85:170] = 0.9
        mock_midas.set_depth_map(depth)
        result = guard.check_path(frame)
        assert result["safe"] is False
        assert result["center_depth"] > guard.min_safe_depth
        assert result["action"] in ("go_left", "go_right")


class TestActionLogic:
    def test_go_left_when_left_clearer(self, mock_midas, guard, frame):
        """When center is blocked and left is clearer, action should be go_left."""
        depth = torch.ones(1, 256, 256) * 0.1
        # Block center and right, leave left clear
        depth[0, 85:170, 85:256] = 0.9  # center + right columns
        mock_midas.set_depth_map(depth)
        result = guard.check_path(frame)
        assert result["safe"] is False
        assert result["action"] == "go_left"

    def test_go_right_when_right_clearer(self, mock_midas, guard, frame):
        """When center is blocked and right is clearer, action should be go_right."""
        depth = torch.ones(1, 256, 256) * 0.1
        # Block center and left, leave right clear
        depth[0, 85:170, 0:170] = 0.9  # left + center columns
        mock_midas.set_depth_map(depth)
        result = guard.check_path(frame)
        assert result["safe"] is False
        assert result["action"] == "go_right"

    def test_reverse_when_all_blocked(self, mock_midas, guard, frame):
        """When all three columns are blocked, action should be reverse."""
        # Need variation so normalization doesn't collapse to zero.
        # Set middle row high (0.9) with a small low-depth region outside
        # the middle row so normalization still puts middle-row values above threshold.
        depth = torch.ones(1, 256, 256) * 0.9  # everything close
        depth[0, 0:10, 0:10] = 0.0  # small far-away corner (outside middle row)
        mock_midas.set_depth_map(depth)
        result = guard.check_path(frame)
        assert result["safe"] is False
        assert result["action"] == "reverse"


class TestCaching:
    def test_cached_result_within_interval(self, mock_midas, frame):
        """Calling check_path twice within check_interval should return cached result."""
        from flightrisk.drone.obstacle import ObstacleGuard
        guard = ObstacleGuard(min_safe_depth=0.35, check_interval=5.0)

        mock_midas.set_depth_map(torch.ones(1, 256, 256) * 0.1)
        result1 = guard.check_path(frame)

        # Change the depth map — but cached result should be returned
        mock_midas.set_depth_map(torch.ones(1, 256, 256) * 0.9)
        result2 = guard.check_path(frame)

        assert result1 == result2
        assert result2["safe"] is True  # still the cached "safe" result

    def test_cache_expires(self, mock_midas, frame):
        """After check_interval expires, a fresh inference should run."""
        from flightrisk.drone.obstacle import ObstacleGuard
        guard = ObstacleGuard(min_safe_depth=0.35, check_interval=0.05)

        mock_midas.set_depth_map(torch.ones(1, 256, 256) * 0.1)
        result1 = guard.check_path(frame)
        assert result1["safe"] is True

        time.sleep(0.1)  # wait for cache to expire

        # Need variation so normalization produces values above threshold
        blocked = torch.ones(1, 256, 256) * 0.9
        blocked[0, 0:10, 0:10] = 0.0  # small far corner for normalization spread
        mock_midas.set_depth_map(blocked)
        result2 = guard.check_path(frame)
        assert result2["safe"] is False


class TestThreadSafety:
    def test_concurrent_calls(self, mock_midas, guard, frame):
        """Multiple threads calling check_path should not raise."""
        mock_midas.set_depth_map(torch.ones(1, 256, 256) * 0.1)
        results = []
        errors = []

        def _call():
            try:
                r = guard.check_path(frame)
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_call) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Errors in threads: {errors}"
        assert len(results) == 10
        # All results should have the correct structure
        for r in results:
            assert "safe" in r
            assert "action" in r


class TestEdgeCases:
    def test_small_frame(self, guard):
        """Small frames should not crash."""
        small = np.zeros((32, 32, 3), dtype=np.uint8)
        result = guard.check_path(small)
        assert "safe" in result
        assert "action" in result

    def test_single_pixel_frame(self, guard):
        """1x1 frame should not crash."""
        tiny = np.zeros((1, 1, 3), dtype=np.uint8)
        result = guard.check_path(tiny)
        assert "safe" in result


class TestDepthVisualization:
    def test_returns_correct_shape(self, guard, frame):
        """get_depth_visualization should return an image matching input dimensions."""
        vis = guard.get_depth_visualization(frame)
        assert vis.shape[0] == frame.shape[0]
        assert vis.shape[1] == frame.shape[1]
        assert vis.shape[2] == 3  # BGR

    def test_concurrent_visualization(self, guard, frame):
        """get_depth_visualization and check_path called concurrently should not raise."""
        errors = []

        def _viz():
            try:
                guard.get_depth_visualization(frame)
            except Exception as e:
                errors.append(e)

        def _check():
            try:
                guard.check_path(frame)
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=_viz))
            threads.append(threading.Thread(target=_check))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(errors) == 0, f"Concurrent errors: {errors}"

    def test_visualization_uses_cached_depth(self, mock_midas, frame):
        """Visualization should use cached depth map, not run inference again."""
        import unittest.mock as mock_module
        from flightrisk.drone.obstacle import ObstacleGuard

        guard = ObstacleGuard(min_safe_depth=0.35, check_interval=5.0)

        guard.check_path(frame)

        with mock_module.patch.object(mock_midas, 'forward', wraps=mock_midas.forward) as spy:
            guard.get_depth_visualization(frame)
            spy.assert_not_called()
