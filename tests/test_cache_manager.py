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


class TestVisionResults:
    PARAMS = "model=m|prompt=p|resolution=512"

    def test_miss_then_hit_after_put(self, tmp_path):
        with CacheManager(tmp_path) as cache:
            assert cache.get_vision(media_file(), 1.5, self.PARAMS) is None
            cache.put_vision(media_file(), 1.5, self.PARAMS, {"moment_type": "social"})
            assert cache.get_vision(media_file(), 1.5, self.PARAMS) == {"moment_type": "social"}

    def test_entries_persist_across_reopen(self, tmp_path):
        with CacheManager(tmp_path) as cache:
            cache.put_vision(media_file(), None, self.PARAMS, {"a": 1})
        with CacheManager(tmp_path) as cache:
            assert cache.get_vision(media_file(), None, self.PARAMS) == {"a": 1}

    def test_timestamp_model_prompt_and_resolution_are_part_of_the_key(self, tmp_path):
        with CacheManager(tmp_path) as cache:
            cache.put_vision(media_file(), 1.5, self.PARAMS, {"a": 1})
            assert cache.get_vision(media_file(), 2.5, self.PARAMS) is None
            assert cache.get_vision(media_file(), None, self.PARAMS) is None
            assert cache.get_vision(media_file(), 1.5, "model=other|prompt=p|resolution=512") is None
            assert cache.get_vision(media_file(), 1.5, "model=m|prompt=p|resolution=768") is None

    def test_several_frames_of_one_file_coexist(self, tmp_path):
        with CacheManager(tmp_path) as cache:
            cache.put_vision(media_file("v.mp4"), 1.0, self.PARAMS, {"a": 1})
            cache.put_vision(media_file("v.mp4"), 4.0, self.PARAMS, {"a": 2})
            assert cache.get_vision(media_file("v.mp4"), 1.0, self.PARAMS) == {"a": 1}
            assert cache.get_vision(media_file("v.mp4"), 4.0, self.PARAMS) == {"a": 2}
            assert cache.count_vision() == 2

    def test_a_timestamp_differing_by_less_than_a_millisecond_is_the_same_frame(self, tmp_path):
        with CacheManager(tmp_path) as cache:
            cache.put_vision(media_file(), 1.0, self.PARAMS, {"a": 1})
            assert cache.get_vision(media_file(), 1.0000001, self.PARAMS) == {"a": 1}

    def test_a_changed_file_is_a_miss(self, tmp_path):
        with CacheManager(tmp_path) as cache:
            cache.put_vision(media_file(), None, self.PARAMS, {"a": 1})
            assert cache.get_vision(media_file(size=11), None, self.PARAMS) is None
            assert cache.get_vision(media_file(mtime_ns=2_000), None, self.PARAMS) is None

    def test_a_moved_media_folder_still_hits(self, tmp_path):
        with CacheManager(tmp_path) as cache:
            cache.put_vision(media_file(root=Path("/home/me/trip")), None, self.PARAMS, {"a": 1})
            assert cache.get_vision(media_file(root=Path("/media")), None, self.PARAMS) == {"a": 1}

    def test_an_older_database_is_reset_and_gets_the_new_table(self, tmp_path):
        with CacheManager(tmp_path) as cache:
            path = cache.path
        with sqlite3.connect(path) as conn:
            conn.execute("DROP TABLE vision")
            conn.execute("PRAGMA user_version = 1")
        with CacheManager(tmp_path) as cache:
            cache.put_vision(media_file(), None, self.PARAMS, {"a": 1})
            assert cache.get_vision(media_file(), None, self.PARAMS) == {"a": 1}


class TestTechnicalAnalysis:
    def test_miss_then_hit_and_a_changed_file_or_parameters_are_a_miss(self, tmp_path):
        with CacheManager(tmp_path) as cache:
            assert cache.get_technical(media_file(), "p1") is None
            cache.put_technical(media_file(), "p1", [{"start": None}])
            assert cache.get_technical(media_file(), "p1") == [{"start": None}]
            assert cache.get_technical(media_file(), "p2") is None
            assert cache.get_technical(media_file(size=11), "p1") is None
            assert cache.count_technical() == 1
