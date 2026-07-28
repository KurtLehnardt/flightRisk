"""Tests for amber.canon.TargetCanon."""

import numpy as np
import pytest

from amber.canon import TargetCanon


@pytest.fixture
def canon(tmp_path):
    """Create a TargetCanon backed by a temp SQLite DB."""
    db_path = str(tmp_path / "test_canon.db")
    c = TargetCanon(db_path=db_path)
    yield c
    c.close()


def _make_image(width=64, height=64, color=(0, 128, 255)):
    """Create a simple solid-color test image."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:] = color
    return img


class TestSetTarget:
    def test_set_target_returns_positive_version_id(self, canon):
        img = _make_image()
        vid = canon.set_target(img)
        assert vid > 0

    def test_multiple_set_target_only_last_is_active(self, canon):
        img1 = _make_image(color=(255, 0, 0))
        img2 = _make_image(color=(0, 255, 0))
        img3 = _make_image(color=(0, 0, 255))
        canon.set_target(img1)
        canon.set_target(img2)
        vid3 = canon.set_target(img3)
        assert canon.active_version_id() == vid3

    def test_operator_id_stored_correctly(self, canon):
        img = _make_image()
        canon.set_target(img, operator_id="operator_alpha")
        active = canon.get_active()
        assert active["operator_id"] == "operator_alpha"

    def test_quality_score_stored_float(self, canon):
        img = _make_image()
        canon.set_target(img, quality_score=0.87)
        active = canon.get_active()
        assert abs(active["quality_score"] - 0.87) < 1e-6

    def test_quality_score_none_handled(self, canon):
        img = _make_image()
        canon.set_target(img, quality_score=None)
        active = canon.get_active()
        assert active["quality_score"] is None


class TestGetActive:
    def test_get_active_returns_set_image(self, canon):
        img = _make_image(color=(100, 150, 200))
        canon.set_target(img)
        active = canon.get_active()
        assert active is not None
        assert "image" in active
        assert active["image"].shape == img.shape

    def test_get_active_none_when_empty(self, canon):
        assert canon.get_active() is None


class TestGetHistory:
    def test_get_history_returns_all_newest_first(self, canon):
        for i in range(5):
            canon.set_target(_make_image(color=(i * 50, 0, 0)))
        history = canon.get_history()
        assert len(history) == 5
        # Newest first: ids should be descending
        ids = [h["id"] for h in history]
        assert ids == sorted(ids, reverse=True)

    def test_get_history_limit_works(self, canon):
        for i in range(10):
            canon.set_target(_make_image(color=(i * 25, 0, 0)))
        history = canon.get_history(limit=3)
        assert len(history) == 3


class TestRevertTo:
    def test_revert_to_makes_old_version_active(self, canon):
        img1 = _make_image(color=(255, 0, 0))
        vid1 = canon.set_target(img1)
        canon.set_target(_make_image(color=(0, 255, 0)))
        canon.set_target(_make_image(color=(0, 0, 255)))
        result = canon.revert_to(vid1)
        assert result is not None
        assert canon.active_version_id() == vid1

    def test_revert_to_invalid_returns_none(self, canon):
        assert canon.revert_to(99999) is None


class TestActiveVersionId:
    def test_active_version_id_correct(self, canon):
        img = _make_image()
        vid = canon.set_target(img)
        assert canon.active_version_id() == vid

    def test_active_version_id_none_when_empty(self, canon):
        assert canon.active_version_id() is None


class TestImageRoundtrip:
    def test_image_roundtrip_preserves_shape(self, canon):
        img = _make_image(width=120, height=80)
        canon.set_target(img)
        active = canon.get_active()
        assert active["image"].shape == (80, 120, 3)

    def test_revert_roundtrip_preserves_shape(self, canon):
        img = _make_image(width=100, height=50)
        vid = canon.set_target(img)
        canon.set_target(_make_image())  # set another
        recovered = canon.revert_to(vid)
        assert recovered.shape == (50, 100, 3)
