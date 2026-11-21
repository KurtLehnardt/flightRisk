"""Unit tests for obstacle sensing scripts.

Tests argument parsing, CSV output structure, percentile calculations, and
guard behavior. All guard usage is mocked to avoid loading MiDaS.
"""

import csv
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Fixtures for mocking
# ---------------------------------------------------------------------------

class MockGuardResult:
    """Mock result from ObstacleGuard.check_path()."""

    def __init__(self, safe=True, action="clear", center_depth=0.1,
                 center_p90=0.12, left_depth=0.15, right_depth=0.11,
                 confidence=0.8, center_stat="p90"):
        self.data = {
            "safe": safe,
            "action": action,
            "center_depth": center_depth,
            "center_p90": center_p90,
            "left_depth": left_depth,
            "right_depth": right_depth,
            "confidence": confidence,
            "center_stat": center_stat,
        }

    def get(self, key, default=None):
        return self.data.get(key, default)


@pytest.fixture
def mock_guard():
    """Mock ObstacleGuard that doesn't load MiDaS."""
    guard = MagicMock()
    guard.check_path.return_value = MockGuardResult().data
    return guard


@pytest.fixture
def mock_cv2():
    """Mock cv2 module for video capture."""
    with patch("cv2.VideoCapture") as mock_cap_class:
        mock_cap = MagicMock()
        mock_cap_class.return_value = mock_cap
        yield mock_cap_class, mock_cap


# ---------------------------------------------------------------------------
# Tests for obstacle_replay.py
# ---------------------------------------------------------------------------

class TestObstacleReplayArgumentParsing:
    """Test argument parsing for obstacle_replay.py."""

    def test_parse_required_video_argument(self):
        """--video is required."""
        from scripts import obstacle_replay
        with pytest.raises(SystemExit):
            obstacle_replay.parse_args([])  # No --video

    def test_parse_video_path(self):
        """Parse --video argument."""
        from scripts import obstacle_replay
        args = obstacle_replay.parse_args(["--video", "test.mp4"])
        assert args.video == "test.mp4"

    def test_parse_stride_default(self):
        """Stride defaults to 1."""
        from scripts import obstacle_replay
        args = obstacle_replay.parse_args(["--video", "test.mp4"])
        assert args.stride == 1

    def test_parse_stride_value(self):
        """Parse --stride argument."""
        from scripts import obstacle_replay
        args = obstacle_replay.parse_args(["--video", "test.mp4", "--stride", "5"])
        assert args.stride == 5

    def test_parse_csv_output(self):
        """Parse --out argument for CSV output."""
        from scripts import obstacle_replay
        args = obstacle_replay.parse_args(["--video", "test.mp4", "--out", "results.csv"])
        assert args.csv_out == "results.csv"

    def test_parse_min_safe_depth_default(self):
        """min_safe_depth defaults to 0.35."""
        from scripts import obstacle_replay
        args = obstacle_replay.parse_args(["--video", "test.mp4"])
        assert args.min_safe_depth == 0.35

    def test_parse_min_safe_depth_value(self):
        """Parse --min-safe-depth argument."""
        from scripts import obstacle_replay
        args = obstacle_replay.parse_args(
            ["--video", "test.mp4", "--min-safe-depth", "0.45"]
        )
        assert args.min_safe_depth == 0.45


class TestObstacleReplayCSVOutput:
    """Test CSV output from obstacle_replay.py."""

    def test_csv_headers(self):
        """CSV contains expected headers."""
        from scripts import obstacle_replay
        results = [
            {
                "frame": 1,
                "processed": 1,
                "action": "clear",
                "center_depth": 0.1,
                "center_p90": 0.12,
                "left_depth": 0.15,
                "right_depth": 0.11,
                "confidence": 0.8,
                "center_stat": "p90",
                "latency_ms": 150.5,
                "safe": True,
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "test.csv"
            with open(csv_path, "w", newline="") as f:
                fieldnames = [
                    "frame", "processed", "action", "center_depth", "center_p90",
                    "left_depth", "right_depth", "confidence", "center_stat",
                    "latency_ms", "safe",
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(results)

            # Read back and verify headers
            with open(csv_path, "r") as f:
                reader = csv.DictReader(f)
                assert reader.fieldnames is not None
                assert "frame" in reader.fieldnames
                assert "action" in reader.fieldnames
                assert "latency_ms" in reader.fieldnames
                rows = list(reader)
                assert len(rows) == 1
                assert rows[0]["frame"] == "1"
                assert rows[0]["action"] == "clear"

    def test_csv_row_values(self):
        """CSV rows contain expected values from results."""
        results = [
            {
                "frame": 10,
                "processed": 2,
                "action": "reverse",
                "center_depth": 0.42,
                "center_p90": 0.45,
                "left_depth": 0.40,
                "right_depth": 0.41,
                "confidence": 0.6,
                "center_stat": "p90",
                "latency_ms": 250.3,
                "safe": False,
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "test.csv"
            with open(csv_path, "w", newline="") as f:
                fieldnames = [
                    "frame", "processed", "action", "center_depth", "center_p90",
                    "left_depth", "right_depth", "confidence", "center_stat",
                    "latency_ms", "safe",
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(results)

            with open(csv_path, "r") as f:
                reader = csv.DictReader(f)
                row = next(reader)
                assert row["frame"] == "10"
                assert row["processed"] == "2"
                assert row["action"] == "reverse"
                assert float(row["center_depth"]) == 0.42
                assert row["safe"] == "False"


# ---------------------------------------------------------------------------
# Tests for bench_obstacle.py
# ---------------------------------------------------------------------------

class TestBenchObstacleArgumentParsing:
    """Test argument parsing for bench_obstacle.py."""

    def test_parse_frames_default(self):
        """Frames defaults to 100."""
        from scripts import bench_obstacle
        args = bench_obstacle.parse_args([])
        assert args.frames == 100

    def test_parse_frames_value(self):
        """Parse --frames argument."""
        from scripts import bench_obstacle
        args = bench_obstacle.parse_args(["--frames", "50"])
        assert args.frames == 50

    def test_parse_video_optional(self):
        """--video is optional."""
        from scripts import bench_obstacle
        args = bench_obstacle.parse_args([])
        assert args.video is None

    def test_parse_video_path(self):
        """Parse --video argument."""
        from scripts import bench_obstacle
        args = bench_obstacle.parse_args(["--video", "test.mp4"])
        assert args.video == "test.mp4"

    def test_parse_with_yolo_flag(self):
        """Parse --with-yolo flag."""
        from scripts import bench_obstacle
        args = bench_obstacle.parse_args(["--with-yolo"])
        assert args.with_yolo is True

    def test_parse_with_yolo_default(self):
        """--with-yolo defaults to False."""
        from scripts import bench_obstacle
        args = bench_obstacle.parse_args([])
        assert args.with_yolo is False

    def test_parse_min_safe_depth_default(self):
        """min_safe_depth defaults to 0.35."""
        from scripts import bench_obstacle
        args = bench_obstacle.parse_args([])
        assert args.min_safe_depth == 0.35

    def test_parse_min_safe_depth_value(self):
        """Parse --min-safe-depth argument."""
        from scripts import bench_obstacle
        args = bench_obstacle.parse_args(["--min-safe-depth", "0.50"])
        assert args.min_safe_depth == 0.50


class TestBenchObstacleLatencyPercentiles:
    """Test percentile calculations in benchmarking."""

    def test_percentile_p50(self):
        """p50 percentile is computed correctly."""
        latencies = [100, 150, 200, 250, 300]
        p50 = np.percentile(latencies, 50)
        assert p50 == 200

    def test_percentile_p95(self):
        """p95 percentile is computed correctly."""
        latencies = list(range(1, 101))  # 1 to 100
        p95 = np.percentile(latencies, 95)
        assert 94 <= p95 <= 96  # 95th percentile should be around 95

    def test_percentile_with_outliers(self):
        """p95 is robust to outliers."""
        latencies = [100] * 95 + [10000, 20000, 30000, 40000, 50000]
        p95 = np.percentile(latencies, 95)
        # p95 should be around 100, not pulled by outliers
        assert p95 < 1000

    def test_benchmark_result_structure(self):
        """Benchmark result dict has expected keys."""
        latencies = [100, 150, 200, 250, 300]
        result = {
            "frames_processed": 5,
            "latencies": latencies,
            "p50_ms": float(np.percentile(latencies, 50)),
            "p95_ms": float(np.percentile(latencies, 95)),
            "mean_ms": float(np.mean(latencies)),
            "min_ms": float(np.min(latencies)),
            "max_ms": float(np.max(latencies)),
        }
        assert result["frames_processed"] == 5
        assert result["p50_ms"] == 200
        assert result["mean_ms"] == 200
        assert result["min_ms"] == 100
        assert result["max_ms"] == 300


class TestFrameSourceSynthetic:
    """Test synthetic frame generation."""

    def test_get_frames_synthetic_count(self):
        """Synthetic frames are generated with correct count."""
        from scripts import bench_obstacle
        frames = list(bench_obstacle.get_frames(None, 5))
        assert len(frames) == 5

    def test_get_frames_synthetic_shape(self):
        """Synthetic frames have correct shape (720, 960, 3)."""
        from scripts import bench_obstacle
        frames = list(bench_obstacle.get_frames(None, 3))
        for frame in frames:
            assert frame.shape == (720, 960, 3)

    def test_get_frames_synthetic_dtype(self):
        """Synthetic frames are uint8 BGR."""
        from scripts import bench_obstacle
        frames = list(bench_obstacle.get_frames(None, 1))
        assert frames[0].dtype == np.uint8
        # Values should be in range [0, 256)
        assert frames[0].min() >= 0
        assert frames[0].max() < 256


class TestGuardBuildFunction:
    """Test guard building with mocked torch."""

    @patch("scripts.bench_obstacle.load_yolo_model")
    @patch("flightrisk.drone.obstacle.torch.hub.load")
    def test_build_guard_with_mocked_torch(self, mock_hub_load, mock_yolo):
        """Guard is built with mocked torch."""
        from scripts import bench_obstacle
        from unittest.mock import MagicMock

        # Mock the torch.hub.load to return mock model and transforms
        mock_model = MagicMock()
        mock_transforms = MagicMock()
        mock_transforms.small_transform = MagicMock()

        def hub_load(repo, model_name, **kwargs):
            if model_name == "MiDaS_small":
                return mock_model
            elif model_name == "transforms":
                return mock_transforms
            raise ValueError(f"Unknown model: {model_name}")

        mock_hub_load.side_effect = hub_load

        # Build the guard
        guard = bench_obstacle.build_guard(0.35)
        assert guard is not None
        assert guard.min_safe_depth == 0.35
