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
    ):
        """Record a single match event."""
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            self._conn.execute(
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
    # Cleanup
    # ------------------------------------------------------------------

    def close(self):
        self._conn.close()
