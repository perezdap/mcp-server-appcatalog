"""SQLite-backed HTTP response cache with TTL."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class CacheStore:
    """Thread-safe key-value cache persisted in SQLite.

    Stores JSON-serializable payloads keyed by an arbitrary string. Each entry
    has a created_at timestamp; reads past the configured TTL are treated as
    cache misses and deleted lazily.
    """

    def __init__(self, db_path: Path, ttl_seconds: int) -> None:
        self.db_path = db_path
        self.ttl_seconds = ttl_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Better concurrency for short read/write transactions.
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_created_at ON cache(created_at)"
            )
            conn.commit()

    def get(self, key: str) -> Any | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload, created_at FROM cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
            now = time.time()
            if row is None:
                return None
            if now - row["created_at"] > self.ttl_seconds:
                conn.execute("DELETE FROM cache WHERE cache_key = ?", (key,))
                conn.commit()
                return None
            return json.loads(row["payload"])

    def set(self, key: str, value: Any) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cache (cache_key, payload, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload = excluded.payload,
                    created_at = excluded.created_at
                """,
                (key, json.dumps(value), time.time()),
            )
            conn.commit()

    def delete(self, key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM cache WHERE cache_key = ?", (key,))
            conn.commit()

    def purge_expired(self) -> int:
        cutoff = time.time() - self.ttl_seconds
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM cache WHERE created_at < ?", (cutoff,))
            conn.commit()
            return cursor.rowcount
