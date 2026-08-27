"""Tests for amber.persistence.SessionDB — SQLite session/match/feedback store.

Every test uses its own temp-file database (via the `db` fixture / `tmp_path`)
rather than the project's default `amber_sessions.db`, so running the suite
never touches real session data.
"""

import json
import threading

import pytest

from amber.persistence import SessionDB


@pytest.fixture
def db(tmp_path):
    instance = SessionDB(db_path=tmp_path / "sessions_test.db")
    yield instance
    instance.close()


class TestCreateSession:
    def test_returns_valid_session_id(self, db):
        session_id = db.create_session(source="webcam")
        assert isinstance(session_id, str)
        assert len(session_id) == 36  # canonical uuid4 string length

    def test_session_is_retrievable_with_all_fields(self, db):
        session_id = db.create_session(
            source="tello",
            target_photo_path="/tmp/ref.jpg",
            target_description="blue jacket, red backpack",
        )
        session = db.get_session(session_id)
        assert session is not None
        assert session["id"] == session_id
        assert session["source"] == "tello"
        assert session["target_photo_path"] == "/tmp/ref.jpg"
        assert session["target_description"] == "blue jacket, red backpack"
        assert session["ended_at"] is None

    def test_get_session_missing_returns_none(self, db):
        assert db.get_session("does-not-exist") is None


class TestEndSession:
    def test_end_session_records_aggregate_stats(self, db):
        session_id = db.create_session(source="webcam")
        db.end_session(
            session_id,
            total_frames=100,
            total_detections=12,
            total_matches=2,
            recording_path="/tmp/rec.mp4",
        )
        session = db.get_session(session_id)
        assert session["ended_at"] is not None
        assert session["total_frames"] == 100
        assert session["total_detections"] == 12
        assert session["total_matches"] == 2
        assert session["recording_path"] == "/tmp/rec.mp4"


class TestAddMatch:
    def test_stores_and_retrieves_match_data(self, db):
        session_id = db.create_session(source="webcam")
        match_id = db.add_match(
            session_id=session_id,
            match_type="reid",
            reid_score=0.82,
            face_score=0.0,
            combined_score=0.82,
            gemma_match=False,
            snapshot_path="/tmp/snap.jpg",
            crop_path="/tmp/crop.jpg",
        )
        assert isinstance(match_id, int)

        matches = db.get_session_matches(session_id)
        assert len(matches) == 1
        m = matches[0]
        assert m["id"] == match_id
        assert m["session_id"] == session_id
        assert m["match_type"] == "reid"
        assert m["reid_score"] == pytest.approx(0.82)
        assert m["combined_score"] == pytest.approx(0.82)
        assert m["gemma_match"] == 0
        assert m["snapshot_path"] == "/tmp/snap.jpg"
        assert m["crop_path"] == "/tmp/crop.jpg"


class TestUpdateMatch:
    def test_updates_gemma_fields(self, db):
        session_id = db.create_session(source="webcam")
        match_id = db.add_match(session_id=session_id, match_type="reid", reid_score=0.6)

        db.update_match(
            match_id,
            gemma_match=True,
            gemma_confidence="high",
            reasoning="Same red backpack and blue jacket",
        )

        matches = db.get_session_matches(session_id)
        assert len(matches) == 1
        m = matches[0]
        assert m["gemma_match"] == 1
        assert m["gemma_confidence"] == "high"
        assert m["reasoning"] == "Same red backpack and blue jacket"


class TestGetRecentSessions:
    def test_returns_sessions_newest_first(self, db, monkeypatch):
        timestamps = iter([
            "2026-01-01T10:00:00",
            "2026-01-01T11:00:00",
            "2026-01-01T12:00:00",
        ])
        monkeypatch.setattr(
            "amber.persistence.time.strftime",
            lambda *_args, **_kwargs: next(timestamps),
        )
        id1 = db.create_session(source="webcam")
        id2 = db.create_session(source="tello")
        id3 = db.create_session(source="mavlink")

        recent = db.get_recent_sessions(limit=20)
        assert [s["id"] for s in recent] == [id3, id2, id1]

    def test_respects_limit(self, db):
        for _ in range(5):
            db.create_session(source="webcam")
        recent = db.get_recent_sessions(limit=2)
        assert len(recent) == 2


class TestGetSessionMatches:
    def test_only_returns_matches_for_given_session(self, db):
        s1 = db.create_session(source="webcam")
        s2 = db.create_session(source="tello")
        db.add_match(session_id=s1, match_type="reid", reid_score=0.5)
        db.add_match(session_id=s1, match_type="face", face_score=0.6)
        db.add_match(session_id=s2, match_type="reid", reid_score=0.9)

        s1_matches = db.get_session_matches(s1)
        s2_matches = db.get_session_matches(s2)
        assert len(s1_matches) == 2
        assert len(s2_matches) == 1
        assert all(m["session_id"] == s1 for m in s1_matches)
        assert s2_matches[0]["session_id"] == s2

    def test_empty_for_session_with_no_matches(self, db):
        s1 = db.create_session(source="webcam")
        assert db.get_session_matches(s1) == []


class TestFeedback:
    def test_add_feedback_and_get_feedback_stats(self, db):
        s1 = db.create_session(source="webcam")
        m1 = db.add_match(session_id=s1, match_type="reid", combined_score=0.9)
        m2 = db.add_match(session_id=s1, match_type="reid", combined_score=0.3)
        db.add_feedback(m1, s1, "confirmed", notes="correct match")
        db.add_feedback(m2, s1, "rejected", notes="wrong person")

        stats = db.get_feedback_stats()
        assert stats["total_confirmed"] == 1
        assert stats["total_rejected"] == 1
        assert stats["confirmation_rate"] == pytest.approx(0.5)
        assert stats["avg_confirmed_score"] == pytest.approx(0.9)
        assert stats["avg_rejected_score"] == pytest.approx(0.3)

    def test_get_feedback_stats_with_no_feedback_is_zeroed(self, db):
        stats = db.get_feedback_stats()
        assert stats["total_confirmed"] == 0
        assert stats["total_rejected"] == 0
        assert stats["confirmation_rate"] == 0.0

    def test_get_confirmed_matches(self, db):
        s1 = db.create_session(source="webcam")
        m1 = db.add_match(session_id=s1, match_type="reid", combined_score=0.9)
        db.add_feedback(m1, s1, "confirmed")
        confirmed = db.get_confirmed_matches()
        assert len(confirmed) == 1
        assert confirmed[0]["id"] == m1


class TestExportEvalDataset:
    def test_produces_expected_format(self, db, tmp_path):
        s1 = db.create_session(source="webcam")
        m1 = db.add_match(
            session_id=s1,
            match_type="reid",
            reid_score=0.8,
            face_score=0.1,
            combined_score=0.8,
            gemma_match=True,
            gemma_confidence="high",
            reasoning="same jacket",
            snapshot_path="/tmp/a.jpg",
            crop_path="/tmp/b.jpg",
        )
        m2 = db.add_match(session_id=s1, match_type="face", combined_score=0.2)
        db.add_feedback(m1, s1, "confirmed", notes="yes")
        db.add_feedback(m2, s1, "rejected", notes="no")

        output_path = tmp_path / "eval.json"
        count = db.export_eval_dataset(str(output_path))
        assert count == 2

        with open(output_path) as f:
            dataset = json.load(f)

        assert len(dataset) == 2
        by_id = {row["match_id"]: row for row in dataset}
        assert by_id[m1]["is_match"] is True
        assert by_id[m1]["match_type"] == "reid"
        assert by_id[m1]["gemma_match"] is True
        assert by_id[m1]["feedback_notes"] == "yes"
        assert by_id[m2]["is_match"] is False
        assert by_id[m2]["feedback_notes"] == "no"

    def test_empty_dataset_when_no_feedback(self, db, tmp_path):
        db.create_session(source="webcam")
        output_path = tmp_path / "eval_empty.json"
        count = db.export_eval_dataset(str(output_path))
        assert count == 0
        with open(output_path) as f:
            assert json.load(f) == []


class TestMatchStats:
    def test_aggregates_by_match_type(self, db):
        s1 = db.create_session(source="webcam")
        db.add_match(session_id=s1, match_type="reid", reid_score=0.8, combined_score=0.8)
        db.add_match(session_id=s1, match_type="reid", reid_score=0.6, combined_score=0.6)
        db.add_match(session_id=s1, match_type="face", face_score=0.9, combined_score=0.9)

        stats = db.get_match_stats()
        assert stats["total_matches"] == 3
        assert stats["by_type"]["reid"]["count"] == 2
        assert stats["by_type"]["reid"]["avg_reid_score"] == pytest.approx(0.7)
        assert stats["by_type"]["face"]["count"] == 1


class TestConcurrency:
    def test_concurrent_writes_from_two_threads_do_not_crash(self, db):
        session_id = db.create_session(source="webcam")
        errors = []

        def _writer(n_writes):
            try:
                for i in range(25):
                    db.add_match(session_id=session_id, match_type="reid", reid_score=i / 25)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        t1 = threading.Thread(target=_writer, args=(25,))
        t2 = threading.Thread(target=_writer, args=(25,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not t1.is_alive() and not t2.is_alive()
        assert not errors
        matches = db.get_session_matches(session_id)
        assert len(matches) == 50


class TestClose:
    def test_close_prevents_further_queries(self, tmp_path):
        instance = SessionDB(db_path=tmp_path / "close_test.db")
        instance.create_session(source="webcam")
        instance.close()
        with pytest.raises(Exception):
            instance.create_session(source="webcam")
