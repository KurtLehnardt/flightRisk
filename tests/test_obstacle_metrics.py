"""Tests for the candidate obstacle metrics and ``obstacle_replay.py --metrics-report``.

All depth maps are synthetic; MiDaS is never loaded.
"""

import csv
import io
import json

import cv2
import numpy as np
import pytest

from flightrisk.drone.obstacle import centre_floor_ratio, centre_floor_span, centre_sides_ratio
from scripts import obstacle_record
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
        # Zero false clears is reached only by a threshold below every obstacle value, which blocks every clear frame.
        assert res["safe"]["false_clear"] == 0
        assert res["safe"]["false_block"] == 1.0
        assert res["safe"]["just_below_lowest_obstacle"] is True


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


def write_recording(directory, centre_levels, times=None, height_cm=None):
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
        captured = i / 10 if times is None else times[i - 1]
        rows.append({"index": i, "file": name, "captured_at_s": captured, "arrived_at_s": captured + 0.01,
                     "decode_lag_ms": 10, "wall_time": 0, "wall_clock": ""})
    with open(directory / "frames.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    meta = {"label": directory.name}
    if height_cm is not None:
        meta["height_cm"] = height_cm
    (directory / "meta.json").write_text(json.dumps(meta))


def rows_of(values):
    """Metric rows with the same value under every metric name."""
    return [{name: v for name in replay.METRIC_NAMES} for v in values]


def recording_of(values, times=None, stride=1, height_cm=None):
    return {"rows": rows_of(values), "times": times, "stride": stride, "height_cm": height_cm}


class TestCentreFloorSpan:
    def test_value(self):
        # top 0.1, floor 0.5, centre 0.9: (0.9 - 0.1) / (0.5 - 0.1)
        assert centre_floor_span(make_map(centre=0.9, floor=0.5, top=0.1)) == pytest.approx(2.0)

    def test_depth_shift_breaks_the_ratio_but_not_the_span(self):
        base = make_map(centre=0.9, floor=0.5, top=0.1, rest=0.2)
        shifted = base + 5.0
        assert centre_floor_ratio(base) != pytest.approx(centre_floor_ratio(shifted))
        assert centre_floor_span(shifted) == pytest.approx(centre_floor_span(base))

    def test_scale_invariant(self):
        base = make_map(centre=0.9, floor=0.5, top=0.1)
        assert centre_floor_span(base * 7.0) == pytest.approx(centre_floor_span(base))

    def test_near_zero_denominator_is_none(self):
        assert centre_floor_span(make_map(centre=0.9, floor=0.1, top=0.1)) is None
        assert centre_floor_span(make_map(centre=0.9, floor=0.1 + 1e-9, top=0.1)) is None

    def test_floor_not_nearer_than_top_is_none(self):
        assert centre_floor_span(make_map(centre=0.9, floor=0.1, top=0.6)) is None

    def test_unusable_maps_are_none(self):
        assert centre_floor_span(np.ones(10)) is None
        assert centre_floor_span(None) is None
        m = make_map()
        m[3, 3] = np.nan
        assert centre_floor_span(m) is None

    def test_real_sized_map(self):
        """A 192x256 map (the size MiDaS small hands back for a 4:3 frame) gives all three metrics."""
        m = np.full((192, 256), 0.2)
        m[:38, :] = 0.1
        m[64:128, 85:171] = 0.9
        m[192 - 38:, 51:205] = 0.5
        assert centre_floor_ratio(m) == pytest.approx(0.9 / 0.5)
        assert centre_floor_span(m) == pytest.approx((0.9 - 0.1) / (0.5 - 0.1))
        assert centre_sides_ratio(m) == pytest.approx(0.9 / 0.2)
        metrics = replay.frame_metrics({"center_depth": 0.4}, m)
        assert set(metrics) == set(replay.METRIC_NAMES)
        assert all(v is not None for v in metrics.values())


class TestFitting:
    def test_each_recording_weighs_the_same(self):
        # A short obstacle recording that is all called clear and a long one that is all blocked:
        # pooled frames say 1/11, per recording the false clear is 1/2.
        clear = {"yard": [0.1, 0.2]}
        obstacle = {"short": [0.1], "long": [0.9] * 10}
        res = replay.best_threshold(clear, obstacle)
        best = res["best"]
        assert best["threshold"] == pytest.approx(0.2)
        assert best["false_clear"] == pytest.approx(0.5)
        assert best["pooled_false_clear"] == pytest.approx(1 / 11)
        assert best["per_label_false_clear"] == {"short": 1.0, "long": 0.0}

    def test_per_label_false_block_and_pooled(self):
        clear = {"a": [0.1, 0.1], "b": [0.9] * 8}
        obstacle = {"w": [0.95]}
        best = replay.best_threshold(clear, obstacle)["best"]
        assert best["per_label_false_block"]["a"] == 0.0
        assert best["false_block"] == pytest.approx(best["pooled_false_block"] * 0 + _mean_of(best["per_label_false_block"]))

    def test_margin(self):
        res = replay.best_threshold({"c": [0.1, 0.4]}, {"o": [0.6, 0.9]})
        assert res["margin"] == pytest.approx(0.2)
        overlap = replay.best_threshold({"c": [0.1, 0.7]}, {"o": [0.6, 0.9]})
        assert overlap["margin"] == pytest.approx(-0.1)

    def test_threshold_just_below_lowest_obstacle_is_a_candidate(self):
        res = replay.best_threshold({"c": [0.8, 0.9]}, {"o": [0.1, 0.2]})
        assert res["safe"]["just_below_lowest_obstacle"] is True
        assert res["safe"]["threshold"] < 0.1
        assert res["safe"]["false_clear"] == 0 and res["safe"]["false_block"] == 1.0

    def test_zero_false_clear_at_an_observed_value(self):
        res = replay.best_threshold({"c": [0.1, 0.3]}, {"o": [0.5, 0.9]})
        assert res["safe"]["threshold"] == pytest.approx(0.3)
        assert res["safe"]["false_block"] == 0.0

    def test_midpoint(self):
        best = replay.best_threshold({"c": [0.1, 0.2]}, {"o": [0.6, 0.8]})["best"]
        assert best["threshold"] == pytest.approx(0.2)
        assert best["midpoint"] == pytest.approx(0.4)

    def test_none_share_per_class_and_caveat(self):
        res = replay.best_threshold({"c1": [0.1, None], "c2": [0.2, 0.3]}, {"o": [None, 0.9, 0.8, 0.7]})
        assert res["none_share"]["clear"] == pytest.approx(0.25)
        assert res["none_share"]["obstacle"] == pytest.approx(0.25)
        assert res["caveat"] == "Threshold fitted on 2 clear and 1 obstacle recordings; not validated."

    def test_leave_one_out_exposes_a_threshold_fitted_on_one_recording(self):
        # Fitted on everything the threshold separates perfectly; without the odd recording it does not carry over.
        clear = {"c1": [0.1, 0.2], "c2": [0.15, 0.25]}
        obstacle = {"o1": [0.3, 0.35], "o2": [0.12, 0.14]}
        loo = replay.leave_one_out(clear, obstacle)
        assert loo["per_recording"]["o2"]["held_out_as"] == "obstacle"
        assert loo["per_recording"]["o2"]["false_clear"] == 1.0
        assert loo["false_clear"] == pytest.approx(0.5)

    def test_leave_one_out_needs_both_classes(self):
        loo = replay.leave_one_out({"c": [0.1]}, {"o": [0.9]})
        assert loo["per_recording"] == {"o": None, "c": None}
        assert loo["false_clear"] is None and loo["false_block"] is None

    def test_empty_recordings_are_ignored(self):
        assert replay.best_threshold({"c": []}, {"o": [0.5]}) is None


def _mean_of(mapping):
    return sum(mapping.values()) / len(mapping)


class TestVotingRuns:
    def test_resamples_on_capture_time(self):
        times = [i * 0.1 for i in range(10)]
        segments = replay.vote_segments(list(range(10)), times, 0.35)
        assert segments == [[0, 4, 8]]

    def test_zero_interval_keeps_every_frame(self):
        times = [i * 0.1 for i in range(5)]
        assert replay.vote_segments(list(range(5)), times, 0) == [list(range(5))]

    def test_gap_ends_a_run(self):
        # A refused stretch between 0.4 and 2.0 s: no window may span it.
        times = [0.0, 0.1, 0.2, 0.3, 0.4, 2.0, 2.1, 2.2, 2.3]
        segments = replay.vote_segments(list(range(9)), times, 0)
        assert segments == [[0, 1, 2, 3, 4], [5, 6, 7, 8]]

    def test_no_window_across_a_gap(self):
        times = [0.0, 0.1, 5.0, 5.1]
        segments = replay.vote_segments([True, True, True, True], times, 0)
        assert replay.consecutive_clear_rate_segments(segments, 1.0, 3) is None
        assert replay.consecutive_clear_rate_segments([[0.1, 0.1, 0.1, 0.1]], 0.5, 3) == 1.0

    def test_no_times_is_one_run(self):
        assert replay.vote_segments([1, 2, 3], None, 0.35) == [[1, 2, 3]]
        assert replay.vote_segments([1, 2, 3], [None, None, None], 0.35) == [[1, 2, 3]]

    def test_backwards_time_is_a_gap(self):
        assert replay.vote_segments([1, 2, 3, 4], [0.0, 0.1, 0.05, 0.15], 0) == [[1, 2], [3, 4]]

    def test_runs_do_not_join_across_recordings(self):
        a, b = [0.1, 0.1], [0.1, 0.1]
        assert replay.consecutive_clear_rate_segments([a, b], 0.5, 2) == 1.0  # 2 windows, 2 hits
        assert replay.consecutive_clear_rate_segments([a, b], 0.5, 3) is None  # nothing joins into a 3-window

    def test_clear_run_at_the_end_of_a_recording_counts(self):
        values = [0.9, 0.9, 0.1, 0.1, 0.1]
        assert replay.consecutive_clear_rate(values, 0.5, 3) == pytest.approx(1 / 3)
        assert replay.consecutive_clear_rate(values, 0.5, 2) == pytest.approx(2 / 4)


class TestVotingThreshold:
    def test_identical_data_under_two_metric_names_votes_the_same(self):
        """The voting threshold must not depend on a metric's name: score is fitted like the ratios."""
        clear = [0.1, 0.2, 0.3, 0.2, 0.1, 0.25]
        wall = [0.8, 0.9, 0.7, 0.85, 0.9, 0.2]
        recs = {"yard": recording_of(clear), "wall": recording_of(wall)}
        report = replay.build_report(recs, ["yard"], ["wall"], 0.35, 1.0, 0)
        score, ratio = report["metrics"]["score"], report["metrics"]["centre_floor_ratio"]
        assert score["voting"] == ratio["voting"]
        assert score["voting"]["threshold_source"] == replay.SOURCE_FITTED
        assert score["voting"]["threshold"] == score["separation"]["best"]["threshold"]
        assert score["voting"]["threshold"] != 0.35

    def test_fixed_thresholds_without_separation(self):
        recs = {"yard": recording_of([0.1, 0.2, 0.3])}
        report = replay.build_report(recs, [], [], 0.35, 1.5, 0)
        assert report["metrics"]["score"]["voting"]["threshold"] == 0.35
        assert report["metrics"]["score"]["voting"]["threshold_source"] == replay.SOURCE_FIXED
        assert report["metrics"]["centre_sides_ratio"]["voting"]["threshold"] == 1.5
        assert report["metrics"]["centre_floor_span"]["voting"]["threshold_source"] == replay.SOURCE_FIXED

    def test_only_one_class_is_not_a_separation(self):
        recs = {"yard": recording_of([0.1])}
        report = replay.build_report(recs, ["yard"], [], 0.35, 1.0, 0)
        assert report["metrics"]["score"]["separation"] is None
        assert report["metrics"]["score"]["voting"]["threshold_source"] == replay.SOURCE_FIXED

    def test_text_names_the_source_and_caveat(self):
        recs = {"yard": recording_of([0.1, 0.2]), "wall": recording_of([0.8, 0.9])}
        text = replay.format_report(replay.build_report(recs, ["yard"], ["wall"], 0.35, 1.0, 0))
        assert "(fitted on these recordings)" in text
        assert "N consecutive clear votes (N-of-N)" in text
        assert "Threshold fitted on 1 clear and 1 obstacle recordings; not validated." in text
        assert "Margin (lowest obstacle value minus highest clear value)" in text
        assert "Leave-one-recording-out" in text
        fixed = replay.format_report(replay.build_report(recs, [], [], 0.35, 1.0, 0))
        assert "(fixed)" in fixed and "not validated" not in fixed

    def test_report_gives_sampling_and_heights(self):
        times = [i * 0.1 for i in range(6)]
        recs = {
            "a": recording_of([0.1] * 6, times=times, stride=2, height_cm=100),
            "b": recording_of([0.1] * 6),
        }
        report = replay.build_report(recs, [], [], 0.35, 1.0, 0.35)
        assert report["sampling"]["a"]["stride"] == 2
        assert report["sampling"]["a"]["median_frame_interval_s"] == pytest.approx(0.1)
        assert report["metrics"]["score"]["voting"]["votes"]["a"] == 2
        text = replay.format_report(report)
        assert "unknown" in text and "100" in text

    def test_different_heights_warn(self):
        recs = {"a": recording_of([0.1], height_cm=100), "b": recording_of([0.1], height_cm=130)}
        report = replay.build_report(recs, [], [], 0.35, 1.0)
        assert any("different heights" in w for w in report["warnings"])

    def test_empty_recording_warns(self):
        report = replay.build_report({"a": recording_of([])}, [], [], 0.35, 1.0)
        assert any("n=0" in w for w in report["warnings"])
        assert "Warning: a: no frames" in replay.format_report(report)

    def test_several_labels_vote_apart(self):
        recs = {"a": recording_of([0.1, 0.1]), "b": recording_of([0.1, 0.1]), "c": recording_of([0.9, 0.9])}
        report = replay.build_report(recs, [], [], 0.5, 0.5, 0)
        p = report["metrics"]["score"]["voting"]["p_consecutive_clear"]
        assert p["a"] == {"1": 1.0, "2": 1.0, "3": None}
        assert p["b"] == {"1": 1.0, "2": 1.0, "3": None}
        assert p["c"] == {"1": 0.0, "2": 0.0, "3": None}


class TestParsing2:
    def test_label_with_a_backslash_folder(self):
        assert replay.parse_recordings(["label=rec\\dir"]) == [("label", "rec\\dir")]

    def test_ratio_threshold_needs_report(self):
        with pytest.raises(SystemExit):
            replay.parse_args(["--video", "v.mp4", "--ratio-threshold", "1.2"])

    def test_out_with_report_is_an_error(self, tmp_path):
        with pytest.raises(SystemExit):
            replay.parse_args(["--metrics-report", str(tmp_path), "--out", "x.csv"])

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_min_safe_depth_must_be_finite(self, value):
        with pytest.raises(SystemExit):
            replay.parse_args(["--video", "v.mp4", "--min-safe-depth", value])

    def test_vote_interval_must_be_finite_and_not_negative(self, tmp_path):
        for value in ("-1", "nan"):
            with pytest.raises(SystemExit):
                replay.parse_args(["--metrics-report", str(tmp_path), "--vote-interval-s", value])
        assert replay.parse_args(["--metrics-report", str(tmp_path)]).vote_interval_s == 0.35

    def test_ratio_threshold_default(self, tmp_path):
        assert replay.parse_args(["--metrics-report", str(tmp_path)]).ratio_threshold == 1.0

    def test_clear_without_obstacle_warns(self, tmp_path, capsys):
        replay.parse_args(["--metrics-report", f"yard={tmp_path}", "--clear", "yard"])
        assert "Warning: --clear and --obstacle must both be given" in capsys.readouterr().err

    def test_record_height_arg(self):
        assert obstacle_record.parse_args(["--out", "x", "--height-cm", "130"]).height_cm == 130
        assert obstacle_record.parse_args(["--out", "x"]).height_cm is None
        for bad in ("0", "-5", "nan"):
            with pytest.raises(SystemExit):
                obstacle_record.parse_args(["--out", "x", "--height-cm", bad])


class TestCli:
    def test_report_end_to_end(self, tmp_path, monkeypatch, capsys):
        write_recording(tmp_path / "rec" / "yard_clear", [30, 40, 50, 60], height_cm=100)
        write_recording(tmp_path / "rec" / "yard_wall", [200, 220, 240, 250], height_cm=100)
        monkeypatch.setattr(replay, "build_guard", lambda t: FakeGuard())
        monkeypatch.chdir(tmp_path)
        before = sorted(str(p) for p in tmp_path.rglob("*"))
        replay.main([
            "--metrics-report", "rec/yard_clear", "rec/yard_wall",
            "--clear", "yard_clear", "--obstacle", "yard_wall", "--json", "report.json",
        ])
        text = capsys.readouterr().out
        for needle in ("== score", "== centre_floor_ratio", "== centre_floor_span", "yard_clear", "yard_wall",
                       "Clear rate by threshold", "Best separating threshold",
                       "consecutive clear votes (N-of-N)", "N=3", "not validated", "(fitted on these recordings)"):
            assert needle in text
        report = json.loads((tmp_path / "report.json").read_text())
        score = report["metrics"]["score"]
        assert score["summary"]["yard_clear"]["n"] == 4
        assert score["separation"]["best"]["false_clear"] == 0
        assert score["separation"]["best"]["false_block"] == 0
        assert score["separation"]["caveat"] == "Threshold fitted on 1 clear and 1 obstacle recordings; not validated."
        assert report["sampling"]["yard_clear"]["height_cm"] == 100
        # Only the report JSON is new: no images or crops are written. No list in it is longer than the
        # threshold grid, so no per-frame values are in it either.
        after = sorted(str(p) for p in tmp_path.rglob("*"))
        assert sorted(set(after) - set(before)) == [str(tmp_path / "report.json")]
        longest_grid = max(len(g) for g in replay.SWEEP_GRIDS.values())

        def lists(node):
            if isinstance(node, list):
                yield node
                for item in node:
                    yield from lists(item)
            elif isinstance(node, dict):
                for item in node.values():
                    yield from lists(item)

        assert all(len(item) <= longest_grid for item in lists(report))

    def test_stride_decodes_only_the_frames_it_uses(self, tmp_path, monkeypatch):
        write_recording(tmp_path / "a", [30, 40, 50, 60, 70])
        monkeypatch.setattr(replay, "build_guard", lambda t: FakeGuard())
        reads = []
        real = cv2.imread
        monkeypatch.setattr(replay.cv2, "imread", lambda path, *a: reads.append(path) or real(path, *a))
        report = replay.run_metrics_report([("a", str(tmp_path / "a"))], 2, 0.35, out=io.StringIO())
        assert report["metrics"]["score"]["summary"]["a"]["n"] == 3
        assert len(reads) == 3
        assert report["sampling"]["a"]["stride"] == 2
        assert report["sampling"]["a"]["median_frame_interval_s"] == pytest.approx(0.2)

    def test_gap_in_a_recording_is_not_voted_across(self, tmp_path, monkeypatch):
        times = [0.0, 0.1, 0.2, 5.0, 5.1, 5.2]
        write_recording(tmp_path / "a", [30] * 6, times=times)
        monkeypatch.setattr(replay, "build_guard", lambda t: FakeGuard())
        report = replay.run_metrics_report([("a", str(tmp_path / "a"))], 1, 0.35, vote_interval_s=0, out=io.StringIO())
        # Two runs of 3 votes around a 4.8 s gap; the gap does not change the median frame interval.
        assert report["sampling"]["a"]["median_frame_interval_s"] == pytest.approx(0.1)
        assert report["metrics"]["score"]["voting"]["votes"]["a"] == 6

    def test_missing_index_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(replay, "build_guard", lambda t: pytest.fail("the guard must not be built"))
        with pytest.raises(SystemExit):
            replay.run_metrics_report([("a", str(tmp_path))], 1, 0.35, out=io.StringIO())

    def test_partial_recordings_fail_before_the_guard_is_built(self, tmp_path, monkeypatch, capsys):
        write_recording(tmp_path / "good", [30, 40])
        write_recording(tmp_path / "missing_png", [30, 40])
        (tmp_path / "missing_png" / "frame_000002.png").unlink()
        write_recording(tmp_path / "no_rows", [30])
        (tmp_path / "no_rows" / "frames.csv").write_text("index,file,captured_at_s\n")
        monkeypatch.setattr(replay, "build_guard", lambda t: pytest.fail("the guard must not be built"))
        recordings = [(n, str(tmp_path / n)) for n in ("good", "missing_png", "no_rows")]
        with pytest.raises(SystemExit):
            replay.run_metrics_report(recordings, 1, 0.35, out=io.StringIO())
        err = capsys.readouterr().err
        assert "missing_png: 1 of 2 frame files" in err and "partial" in err
        assert "no_rows" in err and "lists no frames" in err
        assert "good:" not in err

    def test_json_folder_must_exist(self, tmp_path, monkeypatch, capsys):
        write_recording(tmp_path / "a", [30])
        monkeypatch.setattr(replay, "build_guard", lambda t: pytest.fail("the guard must not be built"))
        with pytest.raises(SystemExit):
            replay.run_metrics_report([("a", str(tmp_path / "a"))], 1, 0.35,
                                      json_out=str(tmp_path / "nope" / "r.json"), out=io.StringIO())
        assert "--json: the folder" in capsys.readouterr().err

    def test_overwriting_the_json_warns(self, tmp_path, monkeypatch, capsys):
        write_recording(tmp_path / "a", [30, 40])
        monkeypatch.setattr(replay, "build_guard", lambda t: FakeGuard())
        target = tmp_path / "r.json"
        target.write_text("old")
        replay.run_metrics_report([("a", str(tmp_path / "a"))], 1, 0.35, json_out=str(target), out=io.StringIO())
        assert "exists and is overwritten" in capsys.readouterr().err
        assert json.loads(target.read_text())["labels"] == ["a"]

    def test_height_is_read_from_meta(self, tmp_path):
        write_recording(tmp_path / "a", [30], height_cm=130)
        write_recording(tmp_path / "b", [30])
        assert replay.read_height_cm(tmp_path / "a") == 130.0
        assert replay.read_height_cm(tmp_path / "b") is None
        (tmp_path / "b" / "meta.json").write_text("not json")
        assert replay.read_height_cm(tmp_path / "b") is None
