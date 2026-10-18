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

SCHEMA_VERSION = 1
DB_NAME = "cache.sqlite3"

_SCHEMA = """
CREATE TABLE metadata (
    rel_path BLOB PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    params TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


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
            self._conn.execute("DROP TABLE IF EXISTS metadata")
            self._conn.execute(_SCHEMA)
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

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CacheManager":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
