import json
import subprocess
from datetime import datetime

import numpy as np
import pytest
from PIL import Image

from vacation_memories import synthetic_media as sm


def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, check=True, stdin=subprocess.DEVNULL,
    ).stdout
    return json.loads(out)


def mean_color(img):
    return np.asarray(img.convert("RGB")).reshape(-1, 3).mean(axis=0)


def test_scene_frame_is_deterministic():
    a = sm.render_scene_frame(3, 160, 90, seed=1)
    b = sm.render_scene_frame(3, 160, 90, seed=1)
    assert a.size == (160, 90)
    assert np.array_equal(np.asarray(a), np.asarray(b))


def test_different_scenes_look_different():
    frames = [np.asarray(sm.render_scene_frame(i, 160, 90, seed=1)).astype(int) for i in range(6)]
    for i in range(6):
        for j in range(i + 1, 6):
            assert np.abs(frames[i] - frames[j]).mean() > 10, (i, j)


def test_blur_and_brightness_parameters_change_the_frame():
    sharp = sm.render_scene_frame(2, 160, 90, seed=1)
    blurred = sm.render_scene_frame(2, 160, 90, seed=1, blur=4)
    dark = sm.render_scene_frame(2, 160, 90, seed=1, brightness=0.2)
    assert np.asarray(sharp).std() > np.asarray(blurred).std()
    assert mean_color(dark).mean() < mean_color(sharp).mean() * 0.4


def test_make_photo_writes_jpeg_with_exif_date_and_orientation(tmp_path):
    path = tmp_path / "a.jpg"
    when = datetime(2023, 7, 14, 9, 30, 5)
    sm.make_photo(path, scene_id=1, width=200, height=100, capture_datetime=when, exif_orientation=6)
    with Image.open(path) as img:
        assert img.size == (200, 100)
        exif = img.getexif()
        assert exif[0x0112] == 6
        assert exif.get_ifd(0x8769)[0x9003] == "2023:07:14 09:30:05"


def test_make_photo_without_date_has_no_exif_date(tmp_path):
    path = tmp_path / "a.png"
    sm.make_photo(path, scene_id=1, width=64, height=48)
    with Image.open(path) as img:
        assert img.size == (64, 48)
        assert 0x9003 not in img.getexif().get_ifd(0x8769)


def test_make_photo_writes_heic(tmp_path):
    path = tmp_path / "a.heic"
    sm.make_photo(path, scene_id=1, width=64, height=48)
    assert path.stat().st_size > 0


def test_make_video_has_requested_geometry_duration_and_container_date(tmp_path):
    path = tmp_path / "v.mp4"
    sm.make_video(
        path, scenes=[(1, 1.0), (2, 1.0)], width=160, height=90, fps=10,
        creation_time=datetime(2023, 7, 15, 18, 0, 0),
    )
    info = ffprobe(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (160, 90)
    assert float(info["format"]["duration"]) == pytest.approx(2.0, abs=0.2)
    assert info["format"]["tags"]["creation_time"].startswith("2023-07-15T18:00:00")


def test_make_video_scenes_are_distinguishable_in_time(tmp_path):
    path = tmp_path / "v.mkv"
    sm.make_video(path, scenes=[(1, 1.0), (4, 1.0)], width=160, height=90, fps=10)
    expected = [mean_color(sm.render_scene_frame(s, 160, 90, seed=0)) for s in (1, 4)]
    decoded = []
    for t in (0.4, 1.6):
        raw = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(t), "-i", str(path),
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True, stdin=subprocess.DEVNULL,
        ).stdout
        decoded.append(np.frombuffer(raw, np.uint8).reshape(90, 160, 3).reshape(-1, 3).mean(axis=0))
    err = [[np.abs(d - e).sum() for e in expected] for d in decoded]
    assert err[0][0] < err[0][1] / 3
    assert err[1][1] < err[1][0] / 3


def test_make_video_with_audio_and_rotation(tmp_path):
    path = tmp_path / "r.mp4"
    sm.make_video(path, scenes=[(1, 1.0)], width=160, height=90, fps=10, audio=True, rotation=90)
    info = ffprobe(path)
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    side = {d["side_data_type"]: d for d in video.get("side_data_list", [])}
    assert abs(side["Display Matrix"]["rotation"]) == 90


def test_populate_creates_the_reference_set_and_manifest(tmp_path):
    manifest = sm.populate(tmp_path / "media", seed=7)
    kinds = {m.expected_kind for m in manifest}
    assert kinds == {"photo", "video", None}
    for item in manifest:
        assert (tmp_path / "media" / item.rel_path).exists()
    assert any(m.rel_path.suffix == ".heic" for m in manifest)
    assert any(m.expected_kind is None and m.rel_path.suffix == ".jpg" for m in manifest), "corrupt file"
    assert len({m.rel_path.parent for m in manifest}) > 1
    again = sm.populate(tmp_path / "media2", seed=7)
    assert [m.rel_path for m in again] == [m.rel_path for m in manifest]


def test_main_populates_the_folder(tmp_path, capsys):
    assert sm.main([str(tmp_path / "out"), "--seed", "3"]) == 0
    assert any((tmp_path / "out").rglob("*.mp4"))
