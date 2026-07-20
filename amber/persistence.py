"""SQLite session persistence for Amber Drone match history.

Stores search sessions and match results so history survives
dashboard restarts. Thread-safe for use from Flask + background threads.
"""

import sqlite3
import threading
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "amber_sessions.db"

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    source TEXT,
    target_photo_path TEXT,
    target_description TEXT,
    total_frames INTEGER DEFAULT 0,
    total_detections INTEGER DEFAULT 0,
    total_matches INTEGER DEFAULT 0,
    recording_path TEXT
);
"""

_CREATE_MATCHES = """
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    match_type TEXT NOT NULL,
    reid_score REAL DEFAULT 0,
    face_score REAL DEFAULT 0,
    combined_score REAL DEFAULT 0,
    gemma_match INTEGER DEFAULT 0,
    gemma_confidence TEXT,
    reasoning TEXT,
    snapshot_path TEXT,
    crop_path TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
"""

_CREATE_MATCH_FEEDBACK = """
CREATE TABLE IF NOT EXISTS match_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    feedback TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    notes TEXT,
    FOREIGN KEY (match_id) REFERENCES matches(id),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
"""


class SessionDB:
    """Thread-safe SQLite persistence for search sessions and matches."""

    def __init__(self, db_path: str | Path | None = None):
        self._db_path = str(db_path or DB_PATH)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._create_tables()

    def _create_tables(self):
        with self._lock:
            self._conn.execute(_CREATE_SESSIONS)
            self._conn.execute(_CREATE_MATCHES)
            self._conn.execute(_CREATE_MATCH_FEEDBACK)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def create_session(
        self,
        source: str,
        target_photo_path: str | None = None,
        target_description: str | None = None,
    ) -> str:
        """Create a new search session. Returns the session UUID."""
        session_id = str(uuid.uuid4())
        started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            self._conn.execute(
                """INSERT INTO sessions
                   (id, started_at, source, target_photo_path, target_description)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, started_at, source, target_photo_path, target_description),
            )
            self._conn.commit()
        return session_id

    def end_session(
        self,
        session_id: str,
        total_frames: int = 0,
        total_detections: int = 0,
        total_matches: int = 0,
        recording_path: str | None = None,
    ):
        """Mark a session as ended and record aggregate stats."""
        ended_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            self._conn.execute(
                """UPDATE sessions
                   SET ended_at = ?, total_frames = ?, total_detections = ?,
                       total_matches = ?, recording_path = ?
                   WHERE id = ?""",
                (ended_at, total_frames, total_detections, total_matches,
                 recording_path, session_id),
            )
            self._conn.commit()

    def get_session(self, session_id: str) -> dict | None:
        """Return a single session as a dict, or None."""
        cur = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_recent_sessions(self, limit: int = 20) -> list[dict]:
        """Return the most recent sessions, newest first."""
        cur = self._conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Matches
    # ------------------------------------------------------------------

    def add_match(
        self,
        session_id: str,
        match_type: str,
        reid_score: float = 0,
        face_score: float = 0,
        combined_score: float = 0,
        gemma_match: bool = False,
        gemma_confidence: str | None = None,
        reasoning: str | None = None,
        snapshot_path: str | None = None,
        crop_path: str | None = None,
    ) -> int:
        """Record a single match event. Returns the match ID."""
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO matches
                   (session_id, timestamp, match_type, reid_score, face_score,
                    combined_score, gemma_match, gemma_confidence, reasoning,
                    snapshot_path, crop_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, timestamp, match_type,
                    reid_score, face_score, combined_score,
                    int(gemma_match), gemma_confidence, reasoning,
                    snapshot_path, crop_path,
                ),
            )
            self._conn.commit()
            return cursor.lastrowid

    def get_session_matches(self, session_id: str) -> list[dict]:
        """Return all matches for a given session."""
        cur = self._conn.execute(
            "SELECT * FROM matches WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_match_stats(self) -> dict:
        """Aggregate match statistics across all sessions."""
        total = self._conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

        avg_cur = self._conn.execute(
            """SELECT match_type,
                      COUNT(*) AS cnt,
                      AVG(reid_score) AS avg_reid,
                      AVG(face_score) AS avg_face,
                      AVG(combined_score) AS avg_combined
               FROM matches GROUP BY match_type"""
        )
        by_type = {}
        for row in avg_cur.fetchall():
            by_type[row["match_type"]] = {
                "count": row["cnt"],
                "avg_reid_score": round(row["avg_reid"] or 0, 4),
                "avg_face_score": round(row["avg_face"] or 0, 4),
                "avg_combined_score": round(row["avg_combined"] or 0, 4),
            }

        return {
            "total_matches": total,
            "by_type": by_type,
        }

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def add_feedback(
        self,
        match_id: int,
        session_id: str,
        feedback: str,
        notes: str | None = None,
    ):
        """Record operator feedback ('confirmed' or 'rejected') for a match."""
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            self._conn.execute(
                """INSERT INTO match_feedback
                   (match_id, session_id, feedback, timestamp, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (match_id, session_id, feedback, timestamp, notes),
            )
            self._conn.commit()

    def get_feedback_stats(self) -> dict:
        """Aggregate feedback statistics with average scores."""
        confirmed = self._conn.execute(
            "SELECT COUNT(*) FROM match_feedback WHERE feedback = 'confirmed'"
        ).fetchone()[0]
        rejected = self._conn.execute(
            "SELECT COUNT(*) FROM match_feedback WHERE feedback = 'rejected'"
        ).fetchone()[0]
        total = confirmed + rejected
        confirmation_rate = round(confirmed / total, 4) if total > 0 else 0.0

        avg_confirmed = self._conn.execute(
            """SELECT AVG(m.combined_score) FROM match_feedback f
               JOIN matches m ON f.match_id = m.id
               WHERE f.feedback = 'confirmed'"""
        ).fetchone()[0]

        avg_rejected = self._conn.execute(
            """SELECT AVG(m.combined_score) FROM match_feedback f
               JOIN matches m ON f.match_id = m.id
               WHERE f.feedback = 'rejected'"""
        ).fetchone()[0]

        return {
            "total_confirmed": confirmed,
            "total_rejected": rejected,
            "confirmation_rate": confirmation_rate,
            "avg_confirmed_score": round(avg_confirmed or 0, 4),
            "avg_rejected_score": round(avg_rejected or 0, 4),
        }

    def get_confirmed_matches(self, limit: int = 100) -> list[dict]:
        """Return confirmed matches with their scores."""
        cur = self._conn.execute(
            """SELECT m.*, f.timestamp AS feedback_time, f.notes
               FROM match_feedback f
               JOIN matches m ON f.match_id = m.id
               WHERE f.feedback = 'confirmed'
               ORDER BY f.timestamp DESC
               LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

    def export_eval_dataset(self, output_path: str) -> int:
        """Export feedback as a JSON evaluation dataset.

        Each entry maps confirmed -> is_match=True, rejected -> is_match=False.
        Returns the number of exported records.
        """
        cur = self._conn.execute(
            """SELECT m.id, m.match_type, m.reid_score, m.face_score,
                      m.combined_score, m.gemma_match, m.gemma_confidence,
                      m.reasoning, m.snapshot_path, m.crop_path,
                      f.feedback, f.notes AS feedback_notes
               FROM match_feedback f
               JOIN matches m ON f.match_id = m.id
               ORDER BY f.timestamp ASC"""
        )
        rows = cur.fetchall()
        dataset = []
        for row in rows:
            r = dict(row)
            dataset.append({
                "match_id": r["id"],
                "match_type": r["match_type"],
                "reid_score": r["reid_score"],
                "face_score": r["face_score"],
                "combined_score": r["combined_score"],
                "gemma_match": bool(r["gemma_match"]),
                "gemma_confidence": r["gemma_confidence"],
                "reasoning": r["reasoning"],
                "snapshot_path": r["snapshot_path"],
                "crop_path": r["crop_path"],
                "is_match": r["feedback"] == "confirmed",
                "feedback_notes": r["feedback_notes"],
            })

        import json
        with open(output_path, "w") as f:
            json.dump(dataset, f, indent=2)

        return len(dataset)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self):
        self._conn.close()
