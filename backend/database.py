"""
Database Module
===============
SQLite-based event logging for object dwell time sessions.

Schema:
- events table: records each tracked object's appearance session.
  - When an object first appears -> INSERT with first_seen.
  - As it moves through the frame -> UPDATE dwell_seconds.
  - When it leaves -> UPDATE is_active=0, record final dwell_seconds.
- camera_id column added for multi-camera support.

Concurrency:
- The backend runs one processing thread per camera and all of them share this
  single Database instance. Every operation is serialized through a
  threading.RLock, and WAL journal mode plus a busy timeout prevent
  "database is locked" errors under concurrent writes from multiple camera
  threads.
"""

import os
import sqlite3
import threading
import time

# DWELL_DB_PATH env var overrides the default location (used by main.py when
# frozen, which passes an explicit path next to the executable anyway).
DB_PATH = os.environ.get("DWELL_DB_PATH") or os.path.join(os.path.dirname(__file__), "dwell_data.db")


class Database:
    """Manage SQLite connection and event CRUD operations (thread-safe)."""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=30,  # wait up to 30s if another thread holds the write lock
        )
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        """Create the events table (and indexes) if it doesn't exist."""
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    camera_id INTEGER DEFAULT 0,
                    track_id INTEGER NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL,
                    dwell_seconds REAL DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # Migrate legacy databases (missing camera_id) BEFORE creating the
            # camera index, otherwise the index creation fails.
            self._migrate_add_camera_id()
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_active ON events(is_active)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_track ON events(track_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_camera ON events(camera_id)")
            self.conn.commit()

    def _migrate_add_camera_id(self):
        """Add camera_id column if missing (legacy DB migration)."""
        try:
            with self.lock:
                self.conn.execute("SELECT camera_id FROM events LIMIT 1")
        except sqlite3.OperationalError:
            with self.lock:
                self.conn.execute("ALTER TABLE events ADD COLUMN camera_id INTEGER DEFAULT 0")
                self.conn.commit()

    def log_first_seen(self, track_id, camera_id=0):
        """Record first appearance of a tracked object. Returns row_id."""
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO events (camera_id, track_id, first_seen, last_seen, dwell_seconds, is_active) "
                "VALUES (?, ?, ?, ?, 0, 1)",
                (int(camera_id), int(track_id), now, now),
            )
            self.conn.commit()
            return cur.lastrowid

    def update_dwell(self, event_id, dwell_seconds, last_seen=None):
        """Update dwell time for an active tracked object."""
        ts = last_seen or time.time()
        with self.lock:
            self.conn.execute(
                "UPDATE events SET dwell_seconds = ?, last_seen = ? WHERE id = ?",
                (dwell_seconds, ts, int(event_id)),
            )
            self.conn.commit()

    def close_event(self, event_id):
        """Mark an event as inactive (object left the frame)."""
        now = time.time()
        with self.lock:
            self.conn.execute(
                "UPDATE events SET is_active = 0, last_seen = ?, "
                "dwell_seconds = CAST(? AS REAL) - CAST(first_seen AS REAL) "
                "WHERE id = ?",
                (now, now, int(event_id)),
            )
            self.conn.commit()

    def get_active_events(self):
        """Retrieve all currently active event records."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT * FROM events WHERE is_active = 1 ORDER BY first_seen"
            )
            return cur.fetchall()

    def get_history(self, limit=100, offset=0):
        """Retrieve historical (completed) event records, newest first."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT * FROM events WHERE is_active = 0 "
                "ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            return cur.fetchall()

    def get_stats(self):
        """Aggregate statistics across all cameras."""
        with self.lock:
            cur = self.conn.execute("""
                SELECT
                    COUNT(*) AS total_logged,
                    COALESCE(AVG(dwell_seconds), 0) AS avg_dwell,
                    COALESCE(MAX(dwell_seconds), 0) AS max_dwell,
                    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active_count,
                    COUNT(DISTINCT camera_id) AS camera_count
                FROM events
            """)
            row = cur.fetchone()
        return {
            "total_logged": row[0],
            "avg_dwell": round(row[1], 1),
            "max_dwell": round(row[2], 1),
            "active_count": row[3] or 0,
            "camera_count": row[4] or 0,
        }

    def upsert_track(self, track_id, camera_id=0):
        """Find an active event for this track_id + camera_id, or create one."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT id FROM events WHERE track_id = ? AND camera_id = ? AND is_active = 1 LIMIT 1",
                (int(track_id), int(camera_id)),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            return self.log_first_seen(track_id, camera_id=camera_id)

    def close(self):
        """Close the database connection."""
        with self.lock:
            if self.conn:
                self.conn.close()
                self.conn = None
