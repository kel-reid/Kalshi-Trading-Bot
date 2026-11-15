"""Single-owner SQLite spool for unacknowledged WebSocket fills."""

import fcntl
import json
import os
import sqlite3


class FillSpool:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = open(self.path + ".lock", "a+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.commit()
        except BaseException:
            self._lock.close()
            raise

    def append(self, fill):
        payload = json.dumps(fill, allow_nan=False, separators=(",", ":"))
        with self._db:
            self._db.execute("INSERT INTO fills (payload) VALUES (?)", (payload,))

    def pending(self, after=0):
        row = self._db.execute("SELECT id, payload FROM fills WHERE id > ? ORDER BY id LIMIT 1", (after,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def acknowledge(self, row_id):
        with self._db:
            self._db.execute("DELETE FROM fills WHERE id = ?", (row_id,))

    def dirty(self):
        return self._db.execute("SELECT 1 FROM state WHERE key = 'dirty'").fetchone() is not None

    def set_dirty(self):
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO state VALUES ('dirty', 'reconciliation required')")

    def clear_dirty(self):
        with self._db:
            self._db.execute("DELETE FROM state WHERE key = 'dirty'")

    def close(self):
        try:
            self._db.close()
        finally:
            self._lock.close()