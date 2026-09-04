"""Target photo canon — versioned, audited target management."""

import base64
import threading
import sqlite3
from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from amber.persistence import SessionDB


class TargetCanon:
    def __init__(self, db_path: str = "amber_sessions.db", session_db: "SessionDB | None" = None):
        """Create a TargetCanon.

        By default opens its own SQLite connection to *db_path*. When
        *session_db* (an ``amber.persistence.SessionDB``) is provided, its
        connection is reused instead -- two independent ``sqlite3.connect()``
        calls against the same database file can otherwise contend and raise
        ``SQLITE_BUSY`` under concurrent writes from Flask + background
        threads. When sharing, ``close()`` is a no-op: the owner of the
        connection (the ``SessionDB``) is responsible for closing it.
        """
        if session_db is not None:
            self._db_path = session_db._db_path
            self._conn = session_db._conn
            self._lock = session_db._lock  # Share the lock too!
            self._owns_conn = False
        else:
            self._db_path = db_path
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._lock = threading.Lock()
            self._owns_conn = True
        self._conn.row_factory = sqlite3.Row
        self._create_table()

    def _create_table(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS target_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                operator_id TEXT DEFAULT 'default',
                quality_score REAL,
                image_b64 TEXT NOT NULL,
                is_active INTEGER DEFAULT 0
            )
        """)
        self._conn.commit()

    def set_target(self, image: np.ndarray, operator_id: str = "default",
                   quality_score: float | None = None) -> int:
        """Store new target version, mark as active. Returns version_id."""
        _, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        b64 = base64.b64encode(buffer).decode("utf-8")
        with self._lock:
            self._conn.execute("UPDATE target_versions SET is_active = 0 WHERE is_active = 1")
            cursor = self._conn.execute(
                "INSERT INTO target_versions (operator_id, quality_score, image_b64, is_active) VALUES (?, ?, ?, 1)",
                (operator_id, quality_score, b64)
            )
            self._conn.commit()
            return cursor.lastrowid

    def get_active(self) -> dict | None:
        """Get active target version with decoded image."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM target_versions WHERE is_active = 1"
            ).fetchone()
        if not row:
            return None
        return self._row_to_dict(row, include_image=True)

    def get_history(self, limit: int = 20) -> list[dict]:
        """Get target history (newest first), metadata only (no full image)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, timestamp, operator_id, quality_score, is_active FROM target_versions ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def revert_to(self, version_id: int) -> np.ndarray | None:
        """Reactivate old version. Returns decoded image or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM target_versions WHERE id = ?", (version_id,)
            ).fetchone()
            if not row:
                return None
            self._conn.execute("UPDATE target_versions SET is_active = 0 WHERE is_active = 1")
            self._conn.execute("UPDATE target_versions SET is_active = 1 WHERE id = ?", (version_id,))
            self._conn.commit()
        img_bytes = base64.b64decode(row["image_b64"])
        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    def active_version_id(self) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM target_versions WHERE is_active = 1"
            ).fetchone()
        return row["id"] if row else None

    def _row_to_dict(self, row, include_image: bool = False) -> dict:
        d = dict(row)
        if include_image and "image_b64" in d:
            img_bytes = base64.b64decode(d["image_b64"])
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            d["image"] = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            del d["image_b64"]
        elif "image_b64" in d:
            del d["image_b64"]
        return d

    def close(self):
        """Close the underlying connection.

        No-op when the connection is shared with a ``SessionDB`` (i.e.
        constructed with ``session_db=``) -- the ``SessionDB`` owns that
        connection's lifecycle and is responsible for closing it.
        """
        if self._owns_conn:
            self._conn.close()
