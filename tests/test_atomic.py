import json
import os

import pytest

from vacation_memories.atomic import atomic_write_bytes, atomic_write_json, atomic_write_text


def test_write_creates_the_file_with_private_permissions(tmp_path):
    target = tmp_path / "plan.json"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"
    assert target.stat().st_mode & 0o777 == 0o600


def test_write_replaces_an_existing_file_and_leaves_no_temporary_files(tmp_path):
    target = tmp_path / "status.json"
    atomic_write_bytes(target, b"old")
    atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_failed_write_keeps_the_previous_content(tmp_path, monkeypatch):
    target = tmp_path / "status.json"
    atomic_write_text(target, "complete")

    def broken_replace(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        atomic_write_text(target, "partial")
    monkeypatch.undo()
    assert target.read_text() == "complete"
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_json_round_trips_names_that_are_not_valid_utf8(tmp_path):
    target = tmp_path / "plan.json"
    undecodable = os.fsdecode(b"holiday/caf\xe9.jpg")
    atomic_write_json(target, {"source": undecodable, "title": "città"})
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == {"source": undecodable, "title": "città"}
    assert os.fsencode(loaded["source"]) == b"holiday/caf\xe9.jpg"
