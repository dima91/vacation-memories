import os
import sqlite3
from pathlib import Path

from vacation_memories.cache_manager import CacheManager
from vacation_memories.media_scanner import MediaFile


def media_file(rel="day1/a.jpg", size=10, mtime_ns=1_000, root=Path("/media")) -> MediaFile:
    return MediaFile(path=root / rel, rel_path=Path(rel), kind="photo", size=size, mtime_ns=mtime_ns)


def test_miss_then_hit_after_put(tmp_path):
    with CacheManager(tmp_path) as cache:
        assert cache.get_metadata(media_file(), "tz=") is None
        cache.put_metadata(media_file(), "tz=", {"width": 10, "date": "2023-07-14T09:30:05"})
        assert cache.get_metadata(media_file(), "tz=") == {"width": 10, "date": "2023-07-14T09:30:05"}


def test_entries_persist_across_reopen(tmp_path):
    with CacheManager(tmp_path) as cache:
        cache.put_metadata(media_file(), "", {"a": 1})
    with CacheManager(tmp_path) as cache:
        assert cache.get_metadata(media_file(), "") == {"a": 1}


def test_changed_file_params_or_stale_entry_is_a_miss(tmp_path):
    with CacheManager(tmp_path) as cache:
        cache.put_metadata(media_file(), "tz=A", {"a": 1})
        assert cache.get_metadata(media_file(size=11), "tz=A") is None
        assert cache.get_metadata(media_file(mtime_ns=2_000), "tz=A") is None
        assert cache.get_metadata(media_file(), "tz=B") is None


def test_put_replaces_the_previous_entry_of_the_same_file(tmp_path):
    with CacheManager(tmp_path) as cache:
        cache.put_metadata(media_file(), "", {"a": 1})
        cache.put_metadata(media_file(size=11), "", {"a": 2})
        assert cache.get_metadata(media_file(), "") is None
        assert cache.get_metadata(media_file(size=11), "") == {"a": 2}
        assert cache.count_metadata() == 1


def test_key_is_relative_so_a_moved_media_folder_still_hits(tmp_path):
    with CacheManager(tmp_path) as cache:
        cache.put_metadata(media_file(root=Path("/home/me/trip")), "", {"a": 1})
        assert cache.get_metadata(media_file(root=Path("/media")), "") == {"a": 1}


def test_non_utf8_relative_path_is_stored_losslessly(tmp_path):
    odd = Path(os.fsdecode(b"caf\xe9.jpg"))
    with CacheManager(tmp_path) as cache:
        cache.put_metadata(media_file(rel=str(odd)), "", {"a": 1})
    with CacheManager(tmp_path) as cache:
        assert cache.get_metadata(media_file(rel=str(odd)), "") == {"a": 1}


def test_database_lives_in_the_workdir_in_wal_mode(tmp_path):
    workdir = tmp_path / "new" / "work"
    with CacheManager(workdir) as cache:
        assert cache.path.parent == workdir
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_incompatible_schema_version_resets_the_cache(tmp_path):
    with CacheManager(tmp_path) as cache:
        cache.put_metadata(media_file(), "", {"a": 1})
        path = cache.path
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 999")
    with CacheManager(tmp_path) as cache:
        assert cache.get_metadata(media_file(), "") is None
        cache.put_metadata(media_file(), "", {"a": 3})
        assert cache.get_metadata(media_file(), "") == {"a": 3}
