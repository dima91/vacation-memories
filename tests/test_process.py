import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from vacation_memories import process
from vacation_memories.process import Interrupted, ShutdownController, ToolError, ToolRunner, parallel_map


def is_running(pid: int) -> bool:
    """Alive and not a zombie (a killed orphan may linger as one until its new parent reaps it)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def wait_for(path: Path, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.02)
    raise AssertionError(f"{path} was not written")


class FakePopen:
    calls: list = []

    def __init__(self, command, **kwargs):
        FakePopen.calls.append((command, kwargs))
        self.pid, self.returncode = 4242, 0

    def communicate(self, timeout=None):
        return b"", b""

    def poll(self):
        return 0


@pytest.fixture
def fake_popen(monkeypatch):
    FakePopen.calls = []
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return FakePopen.calls


def test_ffmpeg_never_reads_stdin_and_runs_quietly(fake_popen):
    ToolRunner().ffmpeg(["-i", "in.mp4", "out.mp4"])
    command, kwargs = fake_popen[0]
    assert command[:2] == ["ffmpeg", "-nostdin"]
    assert {"-hide_banner", "-nostats"} <= set(command)
    assert command[command.index("-loglevel") + 1] == "error"
    assert command[-2:] == ["in.mp4", "out.mp4"] or command[-3:] == ["-i", "in.mp4", "out.mp4"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert "shell" not in kwargs  # an argument list, never a shell


def test_ffprobe_has_no_stdin_either(fake_popen):
    ToolRunner().ffprobe(["-show_format", "file with spaces.mp4"])
    command, kwargs = fake_popen[0]
    assert command[0] == "ffprobe" and "-hide_banner" in command
    assert command[-1] == "file with spaces.mp4"  # one argument, no quoting needed
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_a_tool_that_reads_stdin_sees_end_of_file_and_does_not_hang():
    result = ToolRunner().run(["cat"], timeout=5)
    assert result.stdout == b""


def test_non_zero_exit_raises_with_the_end_of_stderr():
    with pytest.raises(ToolError, match="exit 3.*boom") as error:
        ToolRunner().run(["sh", "-c", "echo boom >&2; exit 3"])
    assert error.value.returncode == 3


def test_missing_program_is_a_tool_error():
    with pytest.raises(ToolError, match="cannot start"):
        ToolRunner().run(["definitely-not-a-program"])


def test_timeout_kills_the_tool():
    started = time.monotonic()
    with pytest.raises(ToolError, match="timed out"):
        ToolRunner(ShutdownController(grace=0.5)).run(["sleep", "30"], timeout=0.5)
    assert time.monotonic() - started < 5


def test_large_stderr_does_not_block_the_tool():
    result = ToolRunner().run(["sh", "-c", "head -c 300000 /dev/zero | tr '\\0' x >&2"], timeout=20)
    assert len(result.stderr) == 300000


def test_interruption_stops_the_tool_and_everything_it_started(tmp_path):
    marker = tmp_path / "grandchild.pid"
    controller = ShutdownController(grace=0.5)
    runner = ToolRunner(controller)
    outcome = {}

    def work():
        try:
            runner.run(["sh", "-c", f"sleep 60 & echo $! > {marker}; wait"])
        except BaseException as error:  # noqa: BLE001 - the test inspects it
            outcome["error"] = error

    thread = threading.Thread(target=work)
    thread.start()
    grandchild = int(wait_for(marker))
    started = time.monotonic()
    controller.request(signal.SIGTERM)
    thread.join(10)
    assert not thread.is_alive()
    assert isinstance(outcome["error"], Interrupted) and outcome["error"].exit_code == 143
    assert time.monotonic() - started < 5  # well under the 10 s docker stop grants
    time.sleep(0.2)
    assert not is_running(grandchild)  # killed together with its parent: no orphan


def test_a_tool_that_ignores_sigterm_is_killed_after_the_grace_period(tmp_path):
    marker = tmp_path / "pid"
    controller = ShutdownController(grace=0.3)
    runner = ToolRunner(controller)
    outcome = {}

    def work():
        try:
            runner.run(["sh", "-c", f"trap '' TERM; echo $$ > {marker}; while true; do sleep 0.1; done"])
        except BaseException as error:  # noqa: BLE001
            outcome["error"] = error

    thread = threading.Thread(target=work)
    thread.start()
    pid = int(wait_for(marker))
    controller.request(signal.SIGINT)
    thread.join(10)
    assert isinstance(outcome["error"], Interrupted) and outcome["error"].exit_code == 130
    assert not is_running(pid)


def test_nothing_starts_after_a_signal():
    controller = ShutdownController()
    controller.request(signal.SIGHUP)
    with pytest.raises(Interrupted) as error:
        ToolRunner(controller).run(["true"])
    assert error.value.exit_code == 129


def test_the_first_signal_wins():
    controller = ShutdownController()
    controller.request(signal.SIGTERM)
    controller.request(signal.SIGINT)
    assert controller.signum == signal.SIGTERM


def test_real_signals_are_caught_and_the_handlers_restored():
    previous = signal.getsignal(signal.SIGTERM)
    with ShutdownController() as controller:
        assert not controller.requested
        os.kill(os.getpid(), signal.SIGTERM)
        deadline = time.monotonic() + 2
        while not controller.requested and time.monotonic() < deadline:
            time.sleep(0.01)
        assert controller.signum == signal.SIGTERM
        with pytest.raises(Interrupted):
            controller.check()
    assert signal.getsignal(signal.SIGTERM) == previous


def test_controller_in_a_worker_thread_does_not_touch_the_handlers():
    seen = {}

    def work():
        before = signal.getsignal(signal.SIGTERM)
        with ShutdownController():  # signal handlers can only be installed from the main thread
            seen["unchanged"] = signal.getsignal(signal.SIGTERM) == before

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert seen == {"unchanged": True}


def test_parallel_map_keeps_the_order_and_runs_concurrently():
    active, peak = 0, 0
    lock = threading.Lock()

    def work(n):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.1)
        with lock:
            active -= 1
        return n * n

    assert parallel_map(work, list(range(6)), 3, ShutdownController()) == [0, 1, 4, 9, 16, 25]
    assert 2 <= peak <= 3


def test_parallel_map_is_sequential_with_one_job():
    order = []
    assert parallel_map(lambda n: order.append(n) or n, [3, 1, 2], 1, ShutdownController()) == [3, 1, 2]
    assert order == [3, 1, 2]


def test_parallel_map_raises_the_first_error():
    def work(n):
        if n == 2:
            raise ValueError("bad item")
        time.sleep(0.05)
        return n

    with pytest.raises(ValueError, match="bad item"):
        parallel_map(work, list(range(5)), 2, ShutdownController())


def test_parallel_map_stops_on_a_signal():
    controller = ShutdownController()
    started = threading.Event()

    def work(n):
        started.set()
        time.sleep(0.2)
        return n

    threading.Timer(0.1, lambda: controller.request(signal.SIGTERM)).start()
    with pytest.raises(Interrupted):
        parallel_map(work, list(range(20)), 2, controller)


def test_interrupted_exit_codes():
    assert Interrupted(signal.SIGINT).exit_code == 130
    assert Interrupted(signal.SIGTERM).exit_code == 143
    assert Interrupted(signal.SIGHUP).exit_code == 129
    assert process.HANDLED_SIGNALS == (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def test_shutdown_callbacks_run_when_the_signal_arrives():
    controller = ShutdownController()
    called = []
    controller.on_shutdown(lambda: called.append(controller.signum))
    assert called == []
    controller.request(signal.SIGTERM)
    assert called == [signal.SIGTERM]
