import logging
import os
from pathlib import Path

import pytest

from vacation_memories.media_scanner import MediaFile, scan_media


def touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_finds_supported_formats_recursively_with_relative_paths(tmp_path):
    for rel in ["a.jpg", "b/c.PNG", "b/d/e.heic", "f.mp4", "g/h.MOV", "g/i.mkv", "j.jpeg"]:
        touch(tmp_path / rel)
    found = scan_media(tmp_path)
    assert {f.rel_path.as_posix() for f in found} == {
        "a.jpg", "b/c.PNG", "b/d/e.heic", "f.mp4", "g/h.MOV", "g/i.mkv", "j.jpeg",
    }
    kinds = {f.rel_path.name: f.kind for f in found}
    assert kinds["a.jpg"] == "photo" and kinds["c.PNG"] == "photo" and kinds["e.heic"] == "photo"
    assert kinds["f.mp4"] == "video" and kinds["h.MOV"] == "video" and kinds["i.mkv"] == "video"
    assert all(f.path == tmp_path / f.rel_path and f.path.is_absolute() for f in found)


def test_ignores_other_files(tmp_path):
    for rel in ["notes.txt", "clip.avi", "photo.jpg.bak", "noextension"]:
        touch(tmp_path / rel)
    assert scan_media(tmp_path) == []


def test_records_size_and_mtime_ns(tmp_path):
    path = touch(tmp_path / "a.jpg", b"12345")
    os.utime(path, ns=(1_600_000_000_123_456_789,) * 2)
    (found,) = scan_media(tmp_path)
    assert isinstance(found, MediaFile)
    assert found.size == 5
    assert found.mtime_ns == 1_600_000_000_123_456_789


def test_order_is_deterministic(tmp_path):
    for rel in ["z.jpg", "a/b.jpg", "a.jpg", "m.mp4"]:
        touch(tmp_path / rel)
    first = [f.rel_path for f in scan_media(tmp_path)]
    assert first == sorted(first, key=os.fsencode)
    assert first == [f.rel_path for f in scan_media(tmp_path)]


def test_non_utf8_file_name_is_kept_losslessly(tmp_path):
    raw = os.fsencode(tmp_path) + b"/caf\xe9.jpg"
    with open(raw, "wb") as handle:
        handle.write(b"x")
    (found,) = scan_media(tmp_path)
    assert os.fsencode(found.rel_path) == b"caf\xe9.jpg"
    assert found.path.exists()


def test_heic_is_skipped_with_a_warning_when_unsupported(tmp_path, caplog):
    touch(tmp_path / "a.heic")
    touch(tmp_path / "b.jpg")
    with caplog.at_level(logging.WARNING):
        found = scan_media(tmp_path, heic=False)
    assert [f.rel_path.name for f in found] == ["b.jpg"]
    assert "HEIC" in caplog.text


def test_heic_is_included_by_default_when_the_decoder_is_available(tmp_path):
    touch(tmp_path / "a.heic")
    assert [f.rel_path.name for f in scan_media(tmp_path)] == ["a.heic"]


def test_missing_or_non_directory_input_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_media(tmp_path / "missing")
    touch(tmp_path / "file.jpg")
    with pytest.raises(NotADirectoryError):
        scan_media(tmp_path / "file.jpg")


def test_unreadable_directory_is_logged_and_skipped(tmp_path, caplog):
    touch(tmp_path / "ok.jpg")
    locked = tmp_path / "locked"
    touch(locked / "hidden.jpg")
    locked.chmod(0)
    try:
        with caplog.at_level(logging.WARNING):
            found = scan_media(tmp_path)
    finally:
        locked.chmod(0o700)
    assert [f.rel_path.name for f in found] == ["ok.jpg"]
    assert "locked" in caplog.text


def test_broken_symlink_is_skipped_and_directory_loops_do_not_hang(tmp_path, caplog):
    touch(tmp_path / "real.jpg")
    (tmp_path / "broken.jpg").symlink_to(tmp_path / "nowhere.jpg")
    (tmp_path / "loop").symlink_to(tmp_path)
    with caplog.at_level(logging.WARNING):
        found = scan_media(tmp_path)
    assert [f.rel_path.name for f in found] == ["real.jpg"]
    assert "broken.jpg" in caplog.text


def test_scanning_does_not_modify_the_tree(tmp_path):
    path = touch(tmp_path / "a" / "b.jpg")
    before = (path.stat().st_mtime_ns, path.parent.stat().st_mtime_ns, sorted(os.listdir(tmp_path)))
    scan_media(tmp_path)
    assert before == (path.stat().st_mtime_ns, path.parent.stat().st_mtime_ns, sorted(os.listdir(tmp_path)))
