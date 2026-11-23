"""Tests for the candidate obstacle metrics and ``obstacle_replay.py --metrics-report``.

All depth maps are synthetic; MiDaS is never loaded.
"""

import csv
import json

import cv2
import numpy as np
import pytest

from flightrisk.drone.obstacle import centre_floor_ratio, centre_sides_ratio
from scripts import obstacle_replay as replay


def make_map(centre=0.5, floor=0.5, top=0.1, rest=0.2, size=30):
    """A map with the given centre-region, floor-band and top-band levels."""
    m = np.full((size, size), rest, dtype=float)
    m[:int(size * 0.2), :] = top
    m[size // 3:2 * size // 3, size // 3:2 * size // 3] = centre
    m[size - int(size * 0.2):, int(size * 0.2):size - int(size * 0.2)] = floor
    return m


class TestCentreFloorRatio:
    def test_ratio_value(self):
        assert centre_floor_ratio(make_map(centre=1.0, floor=0.5)) == pytest.approx(2.0)

    def test_equal_centre_and_floor_is_one(self):
        assert centre_floor_ratio(make_map(centre=0.4, floor=0.4)) == pytest.approx(1.0)

    def test_scale_invariant(self):
        a = centre_floor_ratio(make_map(centre=1.0, floor=0.5))
        b = centre_floor_ratio(make_map(centre=10.0, floor=5.0, top=1.0, rest=2.0))
        assert a == pytest.approx(b)

    def test_zero_floor_is_none(self):
        assert centre_floor_ratio(make_map(floor=0.0, top=0.0)) is None

    def test_negative_floor_is_none(self):
        assert centre_floor_ratio(make_map(floor=-0.3, top=-0.5)) is None

    def test_nan_is_none(self):
        m = make_map()
        m[0, 0] = np.nan
        assert centre_floor_ratio(m) is None

    def test_inf_is_none(self):
        m = make_map()
        m[10, 10] = np.inf
        assert centre_floor_ratio(m) is None

    def test_floor_farther_than_top_is_none(self):
        assert centre_floor_ratio(make_map(floor=0.1, top=0.6)) is None

    def test_wrong_shape_is_none(self):
        assert centre_floor_ratio(np.ones(10)) is None
        assert centre_floor_ratio(np.ones((2, 2))) is None
        assert centre_floor_ratio(None) is None


class TestCentreSidesRatio:
    def test_centre_closer_than_sides(self):
        m = np.full((30, 30), 0.2)
        m[10:20, 10:20] = 0.6
        assert centre_sides_ratio(m) == pytest.approx(3.0)

    def test_zero_sides_is_none(self):
        m = np.zeros((30, 30))
        m[10:20, 10:20] = 0.6
        assert centre_sides_ratio(m) is None

    def test_nan_is_none(self):
        m = np.full((30, 30), 0.2)
        m[0, 0] = np.nan
        assert centre_sides_ratio(m) is None


class TestSweepMath:
    def test_summarize(self):
        s = replay.summarize([1.0, 2.0, 3.0, 4.0, None])
        assert s["n"] == 5
        assert s["none_share"] == pytest.approx(0.2)
        assert (s["min"], s["median"], s["max"]) == (1.0, 2.5, 4.0)

    def test_summarize_all_none(self):
        s = replay.summarize([None, None])
        assert s["none_share"] == 1.0 and s["median"] is None

    def test_clear_rate_counts_none_as_blocked(self):
        assert replay.clear_rate([0.1, 0.5, None, 0.9], 0.5) == pytest.approx(0.5)

    def test_clear_rate_is_inclusive(self):
        assert replay.clear_rate([0.5], 0.5) == 1.0

    def test_clear_rate_empty(self):
        assert replay.clear_rate([], 0.5) is None

    def test_best_threshold_separates_perfectly(self):
        res = replay.best_threshold([0.1, 0.2, 0.3], [0.7, 0.8, 0.9])
        assert res["best"]["false_clear"] == 0 and res["best"]["false_block"] == 0
        assert 0.3 <= res["best"]["threshold"] < 0.7
        assert res["safe"]["false_block"] == 0

    def test_best_threshold_overlap(self):
        res = replay.best_threshold([0.1, 0.2, 0.6], [0.5, 0.8, 0.9])
        best = res["best"]
        assert best["threshold"] == pytest.approx(0.2)
        assert best["false_clear"] == 0
        assert best["false_block"] == pytest.approx(1 / 3)
        # Zero false clears needs a threshold below 0.5, so 0.6 is blocked as well.
        assert res["safe"]["threshold"] == pytest.approx(0.2)

    def test_best_threshold_none_obstacle_values_are_blocked(self):
        res = replay.best_threshold([0.1, 0.2], [None, None])
        assert res["best"]["false_clear"] == 0

    def test_best_threshold_needs_both_sides(self):
        assert replay.best_threshold([], [0.5]) is None
        assert replay.best_threshold([0.5], []) is None

    def test_no_threshold_with_zero_false_clears(self):
        res = replay.best_threshold([0.5, 0.6], [0.1, 0.9])
        assert res["safe"] is None


class TestVotingMath:
    def test_runs_are_not_independent(self):
        # Clears come in a block, so P(3 in a row) is far above p**3.
        values = [0.1] * 5 + [0.9] * 5
        assert replay.clear_rate(values, 0.5) == 0.5
        assert replay.consecutive_clear_rate(values, 0.5, 1) == pytest.approx(0.5)
        assert replay.consecutive_clear_rate(values, 0.5, 3) == pytest.approx(3 / 8)

    def test_alternating_never_two_in_a_row(self):
        values = [0.1, 0.9] * 5
        assert replay.consecutive_clear_rate(values, 0.5, 1) == pytest.approx(0.5)
        assert replay.consecutive_clear_rate(values, 0.5, 2) == 0.0

    def test_none_breaks_a_run(self):
        assert replay.consecutive_clear_rate([0.1, None, 0.1, 0.1], 0.5, 2) == pytest.approx(1 / 3)

    def test_too_short(self):
        assert replay.consecutive_clear_rate([0.1, 0.1], 0.5, 3) is None


class TestParsing:
    def test_labels(self):
        assert replay.parse_recordings(["rec/a", "x=rec/b", "C:\\rec\\c"]) == [
            ("a", "rec/a"), ("x", "rec/b"), ("c", "C:\\rec\\c")]

    def test_duplicate_label(self):
        with pytest.raises(ValueError):
            replay.parse_recordings(["a/x", "b/x"])

    def test_unknown_clear_label_errors(self, tmp_path):
        with pytest.raises(SystemExit):
            replay.parse_args(["--metrics-report", str(tmp_path), "--clear", "nope"])

    def test_json_needs_report(self):
        with pytest.raises(SystemExit):
            replay.parse_args(["--video", "v.mp4", "--json", "x.json"])

    def test_no_source_errors(self):
        with pytest.raises(SystemExit):
            replay.parse_args([])


class FakeGuard:
    """Reads a synthetic depth map out of the frame's grey level, like MiDaS would hand back."""

    def __init__(self):
        self._last_depth_map = None

    def check_path(self, frame):
        grey = frame[:, :, 0].astype(float) / 255.0
        self._last_depth_map = grey
        p95 = np.percentile(grey, 95)
        centre = grey[10:20, 10:20]
        score = float(np.percentile(centre, 90) / p95) if p95 > 1e-6 else 0.0
        return {"center_depth": score, "degenerate": p95 <= 1e-6, "action": "clear"}


def write_recording(directory, centre_levels):
    """A tiny recording in the obstacle_record.py format: PNGs, frames.csv and meta.json."""
    directory.mkdir(parents=True)
    rows = []
    for i, level in enumerate(centre_levels, start=1):
        img = np.full((30, 30), 60, dtype=np.uint8)
        img[:6, :] = 20
        img[24:, 6:24] = 120
        img[10:20, 10:20] = level
        name = f"frame_{i:06d}.png"
        cv2.imwrite(str(directory / name), cv2.merge([img, img, img]))
        rows.append({"index": i, "file": name, "captured_at_s": i / 10, "arrived_at_s": i / 10 + 0.01,
                     "decode_lag_ms": 10, "wall_time": 0, "wall_clock": ""})
    with open(directory / "frames.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (directory / "meta.json").write_text(json.dumps({"label": directory.name}))


class TestCli:
    def test_report_end_to_end(self, tmp_path, monkeypatch, capsys):
        write_recording(tmp_path / "rec" / "yard_clear", [30, 40, 50, 60])
        write_recording(tmp_path / "rec" / "yard_wall", [200, 220, 240, 250])
        monkeypatch.setattr(replay, "build_guard", lambda t: FakeGuard())
        out_json = tmp_path / "report.json"
        before = sorted(p.name for p in tmp_path.rglob("*"))
        replay.main([
            "--metrics-report", str(tmp_path / "rec" / "yard_clear"), str(tmp_path / "rec" / "yard_wall"),
            "--clear", "yard_clear", "--obstacle", "yard_wall", "--json", str(out_json),
        ])
        text = capsys.readouterr().out
        for needle in ("== score", "== centre_floor_ratio", "yard_clear", "yard_wall",
                       "Clear rate by threshold", "Best separating threshold",
                       "consecutive clear frames", "N=3"):
            assert needle in text
        report = json.loads(out_json.read_text())
        score = report["metrics"]["score"]
        assert score["summary"]["yard_clear"]["n"] == 4
        assert score["separation"]["best"]["false_clear"] == 0
        assert score["separation"]["best"]["false_block"] == 0
        # Only the report JSON is new: no images or crops are written.
        after = sorted(p.name for p in tmp_path.rglob("*"))
        assert sorted(set(after) - set(before)) == ["report.json"]

    def test_stride(self, tmp_path, monkeypatch, capsys):
        write_recording(tmp_path / "a", [30, 40, 50, 60, 70])
        monkeypatch.setattr(replay, "build_guard", lambda t: FakeGuard())
        report = replay.run_metrics_report([("a", str(tmp_path / "a"))], 2, 0.35, out=__import__("io").StringIO())
        assert report["metrics"]["score"]["summary"]["a"]["n"] == 3

    def test_missing_index_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(replay, "build_guard", lambda t: FakeGuard())
        with pytest.raises(SystemExit):
            replay.run_metrics_report([("a", str(tmp_path))], 1, 0.35, out=__import__("io").StringIO())
