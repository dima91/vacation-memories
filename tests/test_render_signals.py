import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from render_fakes import halves_photo, probe_file

from vacation_memories.renderer import REPORT_NAME

WORKER = Path(__file__).parent / "render_worker.py"
PHOTOS = [f"p{n}.jpg" for n in range(5)]


@pytest.fixture
def media(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    for name in PHOTOS:
        halves_photo(root / name, (255, 0, 0), (0, 0, 255), size=(1200, 900))
    return root


@pytest.fixture
def workers():
    started = []
    yield started
    for process in started:  # a failing test must not leave a render running
        if process.poll() is None:
            process.kill()
            process.wait()


def start_worker(media, work, output, tmp_path, workers):
    """A render in a subprocess: stdin closed, no DISPLAY, LANG=C, TZ=UTC."""
    environment = {"PATH": os.environ["PATH"], "LANG": "C", "TZ": "UTC", "HOME": str(tmp_path), "PYTHONPATH": str(Path(__file__).parents[1])}
    process = subprocess.Popen(
        [sys.executable, str(WORKER), str(media), str(work), str(output), *PHOTOS],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
    )
    workers.append(process)
    return process


def finished_segments(work: Path) -> list[Path]:
    """Segments that are complete: a file being written has a hidden temporary name."""
    folder = work / "segments"
    return [p for p in folder.glob("*.mp4") if not p.name.startswith(".")] if folder.is_dir() else []


def wait_until_rendering_a_later_segment(work: Path, process, timeout=60.0):
    """One segment is finished and the next is being written (its temporary file is there)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        folder = work / "segments"
        if finished_segments(work) and folder.is_dir() and any(folder.glob(".*.tmp.mp4")):
            return
        assert process.poll() is None, process.stderr.read().decode()
        time.sleep(0.05)
    raise AssertionError("no segment was produced")


def ffmpeg_processes_using(path: Path) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if "ffmpeg" in command.split(" ", 1)[0] and str(path) in command and state != "Z":
            found.append(int(entry.name))
    return found


@pytest.mark.parametrize(
    "signum, code", [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)], ids=["SIGTERM", "SIGINT", "SIGHUP"]
)
def test_a_signal_during_rendering_stops_everything_in_an_orderly_way(media, tmp_path, workers, signum, code):
    work = tmp_path / "work"
    work.mkdir()
    output = tmp_path / "memories.mp4"
    process = start_worker(media, work, output, tmp_path, workers)
    wait_until_rendering_a_later_segment(work, process)

    started = time.monotonic()
    process.send_signal(signum)
    process.wait(timeout=20)
    assert process.returncode == code, process.stderr.read().decode()
    assert time.monotonic() - started < 6  # the shutdown target is 5 s; docker stop grants 10 by default

    assert ffmpeg_processes_using(work) == []  # no orphan FFmpeg
    assert not output.exists() and not (tmp_path / "memories.partial.mp4").exists()
    assert list((work / "tmp").iterdir()) == []

    status = json.loads((work / "status.json").read_text())  # whole, not truncated
    assert status["state"] == "interrupted" and status["pid"] == process.pid
    assert not (work / REPORT_NAME).exists()

    hidden = list((work / "segments").glob(".*"))
    assert hidden == []  # no half-written segment left behind
    for segment in finished_segments(work):
        assert probe_file(segment).video is not None  # every file that is there is complete


def test_a_later_run_reuses_the_finished_segments_and_completes(media, tmp_path, workers):
    work = tmp_path / "work"
    work.mkdir()
    output = tmp_path / "memories.mp4"
    process = start_worker(media, work, output, tmp_path, workers)
    wait_until_rendering_a_later_segment(work, process)
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=20)
    done_before = len(finished_segments(work))
    assert 1 <= done_before < len(PHOTOS)

    again = start_worker(media, work, output, tmp_path, workers)
    again.wait(timeout=180)
    assert again.returncode == 0, again.stderr.read().decode()
    report = json.loads((work / REPORT_NAME).read_text())
    assert report["ok"] and report["segments"]["reused"] == done_before
    assert output.exists() and probe_file(output).format_duration == pytest.approx(3.0 * len(PHOTOS), abs=0.2)
    assert json.loads((work / "status.json").read_text())["state"] == "done"
