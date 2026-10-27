"""Headless runs of the whole CLI in a subprocess: no terminal, no display, `LANG=C`, closed stdin.

Synthetic media only. The process is started the way `cron` or a `docker compose run -T` would.
"""

import hashlib
import importlib.metadata
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from fake_ollama import FakeOllama
from test_render_signals import ffmpeg_processes_using, finished_segments

from vacation_memories import exit_codes
from vacation_memories.media_probe import probe
from vacation_memories.process import ToolRunner
from vacation_memories.synthetic_media import make_photo, make_video, populate_vacation
from vacation_memories.workdir import WorkdirLock

REPO = Path(__file__).parents[1]
QUICK = ["--no-ai", "--duration", "15", "--resolution", "568x320"]


def headless_env(tmp_path: Path, **extra: str) -> dict:
    """No DISPLAY or WAYLAND_DISPLAY, the C locale, UTC and a TMPDIR that cannot be used."""
    environment = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": str(REPO),
        "LANG": "C", "LC_ALL": "C", "TZ": "UTC", "TMPDIR": "/nonexistent",
    }
    environment.update(extra)
    return environment


def start(arguments: list, environment: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "vacation_memories", *map(str, arguments)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
    )


def run_cli(arguments: list, environment: dict, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "vacation_memories", *map(str, arguments)],
        stdin=subprocess.DEVNULL, capture_output=True, env=environment, timeout=timeout,
    )


def explain(result) -> str:
    return result.stderr.decode("utf-8", "replace")[-3000:]


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    populate_vacation(root, photos=10, videos=1, days=2, video_scenes=(3.0,))
    return root


@pytest.fixture
def processes():
    started = []
    yield started
    for process in started:  # a failing test must not leave a run behind
        if process.poll() is None:
            process.kill()
            process.wait()


def test_a_full_run_without_terminal_display_or_usable_tmpdir(media, tmp_path):
    work = tmp_path / "work"
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir(mode=0o500)  # nothing may be written there
    environment = headless_env(tmp_path, TMPDIR=str(system_tmp))
    result = run_cli(["--input", media, "--workdir", work, *QUICK], environment)
    assert result.returncode == exit_codes.OK, explain(result)
    assert (work / "memories.mp4").exists()
    assert list(system_tmp.iterdir()) == []
    assert (work / "tmp").is_dir() and list((work / "tmp").iterdir()) == []  # temporary files lived and died here
    assert b"\r" not in result.stderr and b"\x1b" not in result.stderr  # no progress bars, no ANSI sequences
    assert json.loads((work / "status.json").read_text())["state"] == "done"

    again = run_cli(["--input", media, "--workdir", tmp_path / "work2", *QUICK], headless_env(tmp_path))
    assert again.returncode == exit_codes.OK, explain(again)  # TMPDIR=/nonexistent


def test_sigterm_during_rendering_stops_cleanly_and_resume_completes(tmp_path, processes):
    media = tmp_path / "media"
    populate_vacation(media, photos=30, videos=0, days=2, size=(640, 480))
    work = tmp_path / "work"
    arguments = ["--input", media, "--workdir", work, "--no-ai", "--duration", "60", "--resolution", "720p",
                 "--jobs", "1"]
    process = start(arguments, headless_env(tmp_path))
    processes.append(process)
    _wait_for_rendering(work, process)

    stopped_at = time.monotonic()
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=20)
    assert process.returncode == 128 + signal.SIGTERM, process.stderr.read().decode("utf-8", "replace")
    assert time.monotonic() - stopped_at < 6
    assert ffmpeg_processes_using(work) == []  # no orphan FFmpeg
    for name in ("status.json", "run_config.json", "edit_plan.json", "runtime_profile.json"):
        json.loads((work / name).read_text())  # whole files: none of them truncated
    assert json.loads((work / "status.json").read_text())["state"] == "interrupted"
    assert json.loads((work / "run_config.json").read_text())["state"] == "interrupted"
    assert not (work / "memories.mp4").exists() and not (work / "memories.partial.mp4").exists()
    done_before = len(finished_segments(work))

    resumed = run_cli([*arguments, "--resume"], headless_env(tmp_path))
    assert resumed.returncode == exit_codes.OK, explain(resumed)
    report = json.loads((work / "render_report.json").read_text())
    assert report["ok"] and report["segments"]["reused"] >= done_before >= 1
    assert all(check["ok"] for check in report["checks"])
    assert probe(work / "memories.mp4", ToolRunner()).format_duration == pytest.approx(60, rel=0.05)


def _wait_for_rendering(work: Path, process, timeout: float = 120.0) -> None:
    """At least one segment finished and the next one being written."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        segments = work / "segments"
        if finished_segments(work) and segments.is_dir() and any(segments.glob(".*.tmp.mp4")):
            return
        assert process.poll() is None, process.stderr.read().decode("utf-8", "replace")
        time.sleep(0.05)
    raise AssertionError("the rendering did not start")


def test_sigterm_while_the_model_is_thinking_does_not_wait_for_its_answer(media, tmp_path, processes):
    with FakeOllama(delay=30.0) as server:
        work = tmp_path / "work"
        process = start(["--input", media, "--workdir", work, "--dry-run", "--duration", "15", "--ollama-url",
                         server.url], headless_env(tmp_path))
        processes.append(process)
        deadline = time.monotonic() + 60
        while not server.chat_requests:
            assert process.poll() is None and time.monotonic() < deadline, process.stderr.read().decode()
            time.sleep(0.05)
        stopped_at = time.monotonic()
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=20)
        assert process.returncode == 128 + signal.SIGTERM, process.stderr.read().decode("utf-8", "replace")
        assert time.monotonic() - stopped_at < 6
        assert len(server.chat_requests) == 1
    assert json.loads((work / "status.json").read_text())["state"] == "interrupted"


def test_a_second_instance_on_the_same_workdir_exits_5_and_another_workdir_starts(media, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with WorkdirLock(work):
        result = run_cli(["--input", media, "--workdir", work, "--dry-run", *QUICK], headless_env(tmp_path))
        assert result.returncode == exit_codes.WORKDIR_IN_USE
        message = result.stderr.decode()
        assert "in use by another instance" in message and f"PID {os.getpid()}" in message

        other = run_cli(["--input", media, "--workdir", tmp_path / "other", "--dry-run", *QUICK],
                        headless_env(tmp_path))
        assert other.returncode == exit_codes.OK, explain(other)


@pytest.fixture
def read_only_media(media, tmp_path):
    root = tmp_path / "read-only"
    shutil.copytree(media, root)
    for folder, folders, files in os.walk(root):
        for name in files:
            os.chmod(os.path.join(folder, name), 0o444)
    for folder, folders, files in sorted(os.walk(root), reverse=True):
        os.chmod(folder, 0o555)
    yield root
    for folder, folders, files in os.walk(root):
        os.chmod(folder, 0o755)


def snapshot(root: Path) -> dict:
    """Content and modification time of every file and folder of a tree."""
    found = {}
    for path in sorted(root.rglob("*")) + [root]:
        info = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        found[str(path.relative_to(root))] = (digest, info.st_mtime_ns, stat.S_IMODE(info.st_mode))
    return found


def test_a_read_only_input_is_left_exactly_as_it_was(read_only_media, tmp_path):
    before = snapshot(read_only_media)
    result = run_cli(["--input", read_only_media, "--workdir", tmp_path / "work", *QUICK], headless_env(tmp_path))
    assert result.returncode == exit_codes.OK, explain(result)
    assert snapshot(read_only_media) == before


def test_file_names_that_are_not_utf8_are_handled_without_loss(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    photo_name, video_name = b"caf\xe9.jpg", b"v\xeddeo_\xff.mp4"
    when = datetime(2023, 7, 14, 10, 0, 0)
    make_photo(Path(os.fsdecode(bytes(media) + b"/" + photo_name)), 1, 320, 240, capture_datetime=when, mosaic=True)
    make_video(Path(os.fsdecode(bytes(media) + b"/" + video_name)), [(2, 3.0)], 320, 240, mosaic=True,
               creation_time=when.replace(hour=11))
    for n in range(2):
        make_photo(media / f"plain{n}.jpg", 10 + n, 320, 240, capture_datetime=when.replace(hour=12 + n), mosaic=True)
    work = tmp_path / "work"

    result = run_cli(["--input", media, "--workdir", work, *QUICK], headless_env(tmp_path))
    assert result.returncode == exit_codes.OK, explain(result)
    plan = json.loads((work / "edit_plan.json").read_text())
    names = {os.fsencode(element["source"]) for element in plan["elements"]}
    assert {photo_name, video_name} <= names  # the exact bytes come back from the plan
    assert json.loads((work / "render_report.json").read_text())["ok"]


def test_the_division_into_days_does_not_depend_on_the_time_zone_of_the_machine(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    moments = [datetime(2023, 7, 14, 23, 40), datetime(2023, 7, 15, 0, 20), datetime(2023, 7, 15, 12, 0),
               datetime(2023, 7, 15, 23, 55), datetime(2023, 7, 16, 0, 5), datetime(2023, 7, 16, 9, 0)]
    for number, moment in enumerate(moments):
        make_photo(media / f"p{number}.jpg", 20 + number, 320, 240, capture_datetime=moment, mosaic=True)

    days = []
    for zone in ("UTC", "Pacific/Auckland"):
        work = tmp_path / zone.replace("/", "_")
        result = run_cli(["--input", media, "--workdir", work, "--dry-run", "--structure", "chronological", *QUICK],
                         headless_env(tmp_path, TZ=zone))
        assert result.returncode == exit_codes.OK, explain(result)
        plan = json.loads((work / "edit_plan.json").read_text())
        days.append(sorted((e["source"], e["captured_at"][:10], e["date_source"]) for e in plan["elements"]))
    assert days[0] == days[1]
    assert {day for _, day, _ in days[0]} == {"2023-07-14", "2023-07-15", "2023-07-16"}


def test_the_dependencies_do_not_include_the_gui_opencv():
    declared = []
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in (REPO / name).read_text().splitlines():
            line = line.split("#")[0].strip()
            if line and not line.startswith("-"):
                declared.append(line.split("==")[0].split("[")[0].strip().lower())
    assert "opencv-python-headless" in declared
    assert not {"opencv-python", "opencv-contrib-python"} & set(declared)
    installed = {distribution.metadata["Name"].lower() for distribution in importlib.metadata.distributions()}
    assert "opencv-python-headless" in installed
    assert not {"opencv-python", "opencv-contrib-python"} & installed
