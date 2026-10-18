import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from vacation_memories import metadata_extractor as me
from vacation_memories import synthetic_media as sm
from vacation_memories.cache_manager import CacheManager
from vacation_memories.media_scanner import MediaFile, scan_media


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    manifest = sm.populate(root, seed=1)
    files = {f.rel_path: f for f in scan_media(root)}
    return root, manifest, files


def media_at(path: Path, kind: str, root: Path) -> MediaFile:
    info = path.stat()
    return MediaFile(path, path.relative_to(root), kind, info.st_size, info.st_mtime_ns)


def test_extracted_metadata_matches_the_generator_ground_truth(reference):
    _root, manifest, files = reference
    for truth in manifest:
        if truth.expected_kind is None:
            continue
        meta = me.extract_metadata(files[truth.rel_path], assume_tz="UTC")
        assert meta.kind == truth.expected_kind, truth.rel_path
        assert (meta.width, meta.height) == (truth.width, truth.height), truth.rel_path
        assert meta.date_source == truth.date_source, truth.rel_path
        if truth.capture_datetime is not None:
            assert meta.capture_datetime == truth.capture_datetime, truth.rel_path
            assert meta.assumed_tz is None
        if truth.duration is None:
            assert meta.duration is None and meta.fps is None
        else:
            assert meta.duration == pytest.approx(truth.duration, abs=0.25), truth.rel_path
            assert meta.fps == pytest.approx(10.0)


def test_exif_orientation_and_video_rotation_are_reported(reference):
    _root, _manifest, files = reference
    photo = me.extract_metadata(files[Path("2023-07-14_beach/IMG_0002.jpg")], assume_tz="UTC")
    assert photo.exif_orientation == 6 and photo.rotation == 0
    video = me.extract_metadata(files[Path("2023-07-15_town/VID_0004.mp4")], assume_tz="UTC")
    assert video.rotation in (90, 270) and video.exif_orientation == 1


def test_mtime_fallback_uses_the_assumed_time_zone(reference):
    _root, _manifest, files = reference
    png = files[Path("2023-07-14_beach/IMG_0003.png")]  # mtime: 2023-07-14 12:00 UTC
    utc = me.extract_metadata(png, assume_tz="UTC")
    auckland = me.extract_metadata(png, assume_tz="Pacific/Auckland")
    assert (utc.capture_datetime, utc.date_source, utc.assumed_tz) == (datetime(2023, 7, 14, 12), "mtime", "UTC")
    assert auckland.capture_datetime == datetime(2023, 7, 15, 0, 0)
    assert auckland.assumed_tz == "Pacific/Auckland"


def test_mtime_fallback_defaults_to_the_system_time_zone(reference, monkeypatch):
    _root, _manifest, files = reference
    png = files[Path("2023-07-14_beach/IMG_0003.png")]
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    time.tzset()
    try:
        meta = me.extract_metadata(png)
    finally:
        monkeypatch.undo()
        time.tzset()
    assert meta.capture_datetime == datetime(2023, 7, 15, 0, 0)
    assert meta.assumed_tz.startswith("system:")


def test_embedded_dates_do_not_depend_on_the_system_time_zone(reference, monkeypatch):
    _root, manifest, files = reference
    embedded = [t for t in manifest if t.capture_datetime is not None]
    results = {}
    for zone in ("UTC", "Pacific/Auckland"):
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        results[zone] = [me.extract_metadata(files[t.rel_path]).capture_datetime for t in embedded]
    monkeypatch.undo()
    time.tzset()
    assert results["UTC"] == results["Pacific/Auckland"] == [t.capture_datetime for t in embedded]


def test_invalid_time_zone_is_rejected(reference):
    _root, _manifest, files = reference
    with pytest.raises(ValueError, match="Not/AZone"):
        me.extract_metadata(files[Path("2023-07-14_beach/IMG_0003.png")], assume_tz="Not/AZone")


def test_photo_with_only_the_generic_exif_date(tmp_path):
    path = tmp_path / "a.jpg"
    exif = Image.Exif()
    exif[0x0132] = "2022:01:02 03:04:05"
    Image.new("RGB", (8, 6)).save(path, exif=exif)
    meta = me.extract_metadata(media_at(path, "photo", tmp_path), assume_tz="UTC")
    assert (meta.capture_datetime, meta.date_source) == (datetime(2022, 1, 2, 3, 4, 5), "exif")


def test_invalid_exif_date_falls_back_to_mtime(tmp_path):
    path = tmp_path / "a.jpg"
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "0000:00:00 00:00:00"
    Image.new("RGB", (8, 6)).save(path, exif=exif)
    meta = me.extract_metadata(media_at(path, "photo", tmp_path), assume_tz="UTC")
    assert meta.date_source == "mtime"


def make_tagged_mov(path: Path, *tags: str) -> None:
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x48:d=1:r=5",
               "-pix_fmt", "yuv420p", "-movflags", "use_metadata_tags"]
    for tag in tags:
        command += ["-metadata", tag]
    subprocess.run(command + [str(path)], check=True, stdin=subprocess.DEVNULL, capture_output=True)


def test_container_date_is_kept_as_written_ignoring_the_offset(tmp_path):
    path = tmp_path / "a.mov"
    make_tagged_mov(path, "creation_time=2023-07-15T18:00:00Z")
    meta = me.extract_metadata(media_at(path, "video", tmp_path), assume_tz="Pacific/Auckland")
    assert (meta.capture_datetime, meta.date_source) == (datetime(2023, 7, 15, 18), "container")


def test_apple_local_creation_date_wins_over_utc_creation_time(tmp_path):
    path = tmp_path / "a.mov"
    make_tagged_mov(path, "creation_time=2023-07-15T18:00:00Z", "com.apple.quicktime.creationdate=2023-07-15T20:00:00+0200")
    meta = me.extract_metadata(media_at(path, "video", tmp_path))
    assert meta.capture_datetime == datetime(2023, 7, 15, 20)


def test_epoch_container_date_is_ignored(tmp_path):
    path = tmp_path / "a.mov"
    make_tagged_mov(path, "creation_time=1970-01-01T00:00:00Z")
    meta = me.extract_metadata(media_at(path, "video", tmp_path), assume_tz="UTC")
    assert meta.date_source == "mtime"


def test_corrupt_files_raise_metadata_error(reference, tmp_path):
    root, _manifest, files = reference
    for rel in ("misc/broken.jpg", "misc/broken.mp4"):
        broken = media_at(root / rel, "photo" if rel.endswith("jpg") else "video", root)
        with pytest.raises(me.MetadataError):
            me.extract_metadata(broken, assume_tz="UTC")
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    with pytest.raises(me.MetadataError):
        me.extract_metadata(media_at(empty, "photo", tmp_path), assume_tz="UTC")


def test_audio_only_file_is_not_a_video(tmp_path):
    path = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=1", str(path)],
                   check=True, stdin=subprocess.DEVNULL, capture_output=True)
    with pytest.raises(me.MetadataError, match="video stream"):
        me.extract_metadata(media_at(path, "video", tmp_path), assume_tz="UTC")


def test_metadata_round_trips_through_a_dict(reference):
    _root, _manifest, files = reference
    meta = me.extract_metadata(files[Path("2023-07-15_town/VID_0001.mp4")], assume_tz="UTC")
    assert me.MediaMetadata.from_dict(meta.to_dict()) == meta


def test_load_metadata_skips_unreadable_files_and_logs_them(reference, caplog):
    root, manifest, files = reference
    with CacheManager(root.parent / "work-a") as cache, caplog.at_level(logging.WARNING):
        loaded = me.load_metadata(list(files.values()), cache, assume_tz="UTC")
    good = {t.rel_path for t in manifest if t.expected_kind}
    corrupt = {t.rel_path for t in manifest if t.expected_kind is None and t.rel_path.suffix in (".jpg", ".mp4")}
    assert {m.rel_path for m, _ in loaded} == good
    assert all(str(rel) in caplog.text for rel in corrupt)


def test_load_metadata_uses_the_cache_and_invalidates_on_changed_parameters(reference, monkeypatch, tmp_path):
    _root, _manifest, files = reference
    selection = [files[Path("2023-07-14_beach/IMG_0003.png")], files[Path("2023-07-15_town/VID_0003.mkv")]]
    calls = []
    real = me.extract_metadata
    monkeypatch.setattr(me, "extract_metadata", lambda media, assume_tz=None: calls.append(media) or real(media, assume_tz))

    with CacheManager(tmp_path) as cache:
        first = me.load_metadata(selection, cache, assume_tz="UTC")
        assert len(calls) == 2
        again = me.load_metadata(selection, cache, assume_tz="UTC")
        assert len(calls) == 2 and again == first
        other_zone = me.load_metadata(selection, cache, assume_tz="Pacific/Auckland")
        assert len(calls) == 4
        assert other_zone[0][1].capture_datetime != first[0][1].capture_datetime
