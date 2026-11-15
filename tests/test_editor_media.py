"""Thumbnails, waveforms and per-clip previews of the editor: caches that follow their source. Synthetic media only."""

import json
import os
import time
import wave

import pytest
from editor_fakes import make_media

from vacation_memories.core.process import ToolRunner
from vacation_memories.editor import media
from vacation_memories.editor.errors import ApiError
from vacation_memories.planning.edit_plan import OutputSpec, PlanElement

OUTPUT = OutputSpec(1920, 1080, 30.0)


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    return make_media(tmp_path_factory.mktemp("media-tests"))


@pytest.fixture
def workdir(tmp_path):
    return tmp_path / "project"


def first(root, pattern):
    return next(root.rglob(pattern))


def test_the_thumbnail_of_a_photo_is_a_small_jpeg_in_the_cache(library, workdir):
    photo = first(library / "trip", "*.jpg")
    path = media.thumbnail(workdir, photo, "photo", ToolRunner())
    assert path.parent == workdir / "ui" / "thumbs" and path.read_bytes()[:2] == b"\xff\xd8"
    from PIL import Image

    with Image.open(path) as image:
        assert max(image.size) <= media.THUMB_LONG_SIDE


def test_the_thumbnail_is_made_once_and_again_when_the_photo_changes(library, workdir, tmp_path):
    photo = tmp_path / "p.jpg"
    photo.write_bytes(first(library / "trip", "*.jpg").read_bytes())
    path = media.thumbnail(workdir, photo, "photo", ToolRunner())
    stamp = path.stat().st_mtime_ns
    assert media.thumbnail(workdir, photo, "photo", ToolRunner()).stat().st_mtime_ns == stamp
    time.sleep(0.05)  # file times are coarse: the rewritten thumbnail must not share the first one's
    later = time.time() + 10
    os.utime(photo, (later, later))
    assert media.thumbnail(workdir, photo, "photo", ToolRunner()).stat().st_mtime_ns != stamp


def test_the_thumbnail_of_a_video_is_a_frame_at_the_time_asked(library, workdir):
    video = first(library / "trip", "*.mp4")
    early = media.thumbnail(workdir, video, "video", ToolRunner(), 0.5)
    late = media.thumbnail(workdir, video, "video", ToolRunner(), 4.5)
    assert early != late and early.read_bytes()[:2] == b"\xff\xd8" and early.read_bytes() != late.read_bytes()


def test_a_time_past_the_end_of_the_video_is_an_error_not_an_empty_file(library, workdir):
    with pytest.raises(ApiError) as error:
        media.thumbnail(workdir, first(library / "trip", "*.mp4"), "video", ToolRunner(), 500)
    assert error.value.status == 422
    assert not list((workdir / "ui" / "thumbs").glob("*.tmp"))


def test_an_unreadable_photo_is_a_422(workdir, tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a photo")
    with pytest.raises(ApiError) as error:
        media.thumbnail(workdir, broken, "photo", ToolRunner())
    assert error.value.status == 422 and not list((workdir / "ui" / "thumbs").glob(".*"))


def write_wave(path, samples, rate=8000):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(int(value).to_bytes(2, "little", signed=True) for value in samples))


def test_the_waveform_has_the_peaks_and_the_length(workdir, tmp_path):
    track = tmp_path / "quiet-then-loud.wav"
    write_wave(track, [1000] * 8000 + [20000] * 8000)  # 1 s quiet, 1 s loud
    path = media.waveform(workdir, track, ToolRunner(), buckets=10)
    data = json.loads(path.read_text())
    assert data["duration"] == pytest.approx(2.0, abs=0.05) and len(data["peaks"]) == 10
    assert max(data["peaks"][:4]) < 0.05 and min(data["peaks"][6:]) > 0.55


def test_the_waveform_is_cached_until_the_track_changes(workdir, tmp_path):
    track = tmp_path / "t.wav"
    write_wave(track, [5000] * 8000)
    path = media.waveform(workdir, track, ToolRunner())
    stamp = path.stat().st_mtime_ns
    assert media.waveform(workdir, track, ToolRunner()).stat().st_mtime_ns == stamp
    write_wave(track, [9000] * 16000)
    assert json.loads(media.waveform(workdir, track, ToolRunner()).read_text())["duration"] == pytest.approx(2.0, abs=0.05)


def test_a_track_that_is_not_audio_is_a_422(workdir, tmp_path):
    track = tmp_path / "t.wav"
    track.write_bytes(b"not audio")
    with pytest.raises(ApiError) as error:
        media.waveform(workdir, track, ToolRunner())
    assert error.value.status == 422


def test_the_clip_frame_follows_the_plans_proportions():
    assert media.clip_size(OutputSpec(1920, 1080, 30)) == (640, 360)
    assert media.clip_size(OutputSpec(1080, 1920, 30)) == (360, 640)
    assert media.clip_size(OutputSpec(1080, 1080, 30)) == (360, 360)


def probe_size(path):
    import subprocess

    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,duration", "-of", "json", str(path)], capture_output=True, text=True,
                         check=True).stdout
    stream = json.loads(out)["streams"][0]
    return stream["width"], stream["height"], float(stream["duration"])


def test_the_preview_of_a_photo_lasts_as_long_as_the_photo(library, workdir):
    source = first(library / "trip", "*.jpg")
    element = PlanElement(source.relative_to(library / "trip").as_posix(), "photo", 2.0, id="p")
    path = media.clip_preview(workdir, library / "trip", element, OUTPUT, ToolRunner())
    width, height, duration = probe_size(path)
    assert (width, height) == (640, 360) and duration == pytest.approx(2.0, abs=0.2)
    assert path.parent == workdir / "ui" / "clips" and not list(path.parent.glob(".*"))


def test_the_preview_of_a_moment_is_that_stretch_and_a_trim_replaces_the_old_preview(library, workdir):
    source = first(library / "trip", "*.mp4").relative_to(library / "trip").as_posix()
    full = PlanElement(source, "video", 4.0, start=1.0, end=5.0, id="m")
    path = media.clip_preview(workdir, library / "trip", full, OUTPUT, ToolRunner())
    assert probe_size(path)[2] == pytest.approx(4.0, abs=0.3)
    assert media.clip_preview(workdir, library / "trip", full, OUTPUT, ToolRunner()) == path  # cached
    trimmed = PlanElement(source, "video", 2.0, start=1.0, end=3.0, id="m")  # the same element, trimmed
    again = media.clip_preview(workdir, library / "trip", trimmed, OUTPUT, ToolRunner())
    assert again != path and probe_size(again)[2] == pytest.approx(2.0, abs=0.3)
    assert not path.exists() and len(list(again.parent.glob("*.mp4"))) == 1


def test_the_preview_of_a_clip_whose_file_is_gone_is_a_404(workdir, tmp_path):
    with pytest.raises(ApiError) as error:
        media.clip_preview(workdir, tmp_path, PlanElement("gone.jpg", "photo", 2.0), OUTPUT, ToolRunner())
    assert error.value.status == 404
