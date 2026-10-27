"""Analysis cache in an sqlite3 database inside the working folder.

Entries are keyed by the path relative to the input folder plus size and mtime, so
the cache survives moving the media to another PC or container (as long as mtime is
preserved). WAL mode, single writer process, local filesystem only.
"""

import json
import os
import sqlite3
from pathlib import Path

from vacation_memories.media_scanner import MediaFile

SCHEMA_VERSION = 3
DB_NAME = "cache.sqlite3"

NO_TIMESTAMP_MS = -1  # photos have a single frame


_SCHEMA = (
    """
CREATE TABLE metadata (
    rel_path BLOB PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    params TEXT NOT NULL,
    data TEXT NOT NULL
)
""",
    """
CREATE TABLE vision (
    rel_path BLOB NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    params TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (rel_path, timestamp_ms, params)
)
""",
    """
CREATE TABLE technical (
    rel_path BLOB PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    params TEXT NOT NULL,
    data TEXT NOT NULL
)
""",
)
_TABLES = ("metadata", "vision", "technical")


class CacheManager:
    def __init__(self, workdir: Path):
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        self.path = workdir / DB_NAME
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        with self._conn:
            # The cache only holds derived data: an incompatible version is rebuilt from scratch.
            for table in _TABLES:
                self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            for statement in _SCHEMA:
                self._conn.execute(statement)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def get_metadata(self, media: MediaFile, params: str) -> dict | None:
        """Return the cached metadata, or None if missing or stale.

        `params` holds the settings that change the result (e.g. the assumed time zone).
        """
        row = self._conn.execute(
            "SELECT size, mtime_ns, params, data FROM metadata WHERE rel_path = ?",
            (os.fsencode(media.rel_path),),
        ).fetchone()
        if row is None or row[:3] != (media.size, media.mtime_ns, params):
            return None
        return json.loads(row[3])

    def put_metadata(self, media: MediaFile, params: str, data: dict) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata (rel_path, size, mtime_ns, params, data) VALUES (?, ?, ?, ?, ?)",
                (os.fsencode(media.rel_path), media.size, media.mtime_ns, params, json.dumps(data)),
            )

    def count_metadata(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]

    def get_technical(self, media: MediaFile, params: str) -> list | None:
        """Return the cached candidates of one file (technical analysis), or None if missing or stale.

        `params` holds what changes the analysis (sampling, segment length, signature backend).
        """
        row = self._conn.execute(
            "SELECT size, mtime_ns, params, data FROM technical WHERE rel_path = ?",
            (os.fsencode(media.rel_path),),
        ).fetchone()
        if row is None or row[:3] != (media.size, media.mtime_ns, params):
            return None
        return json.loads(row[3])

    def put_technical(self, media: MediaFile, params: str, data: list) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO technical (rel_path, size, mtime_ns, params, data) VALUES (?, ?, ?, ?, ?)",
                (os.fsencode(media.rel_path), media.size, media.mtime_ns, params, json.dumps(data)),
            )

    def count_technical(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM technical").fetchone()[0]

    def get_vision(self, media: MediaFile, timestamp: float | None, params: str) -> dict | None:
        """Return the cached answer of the vision model for one frame, or None if missing or stale.

        `params` holds what changes the answer (model, prompt, resolution); `timestamp` is seconds
        into a clip, or None for a photo.
        """
        row = self._conn.execute(
            "SELECT size, mtime_ns, data FROM vision WHERE rel_path = ? AND timestamp_ms = ? AND params = ?",
            (os.fsencode(media.rel_path), _timestamp_ms(timestamp), params),
        ).fetchone()
        if row is None or row[:2] != (media.size, media.mtime_ns):
            return None
        return json.loads(row[2])

    def put_vision(self, media: MediaFile, timestamp: float | None, params: str, data: dict) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO vision (rel_path, timestamp_ms, params, size, mtime_ns, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (os.fsencode(media.rel_path), _timestamp_ms(timestamp), params, media.size, media.mtime_ns,
                 json.dumps(data)),
            )

    def count_vision(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM vision").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CacheManager":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _timestamp_ms(timestamp: float | None) -> int:
    return NO_TIMESTAMP_MS if timestamp is None else round(timestamp * 1000)
