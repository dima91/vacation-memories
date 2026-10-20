"""Running FFmpeg and FFprobe: no stdin, own process group, orderly shutdown on signals.

Every tool starts with `-nostdin` (FFmpeg) and `stdin=DEVNULL`, so it can neither hang nor eat the
input of a background or `cron` job, and runs in its own session so that one `killpg` takes down the
tool together with anything it spawned. `ShutdownController` turns SIGINT, SIGTERM and SIGHUP into an
orderly stop: the children are terminated at once (killed after a short grace period), the running
unit of work raises `Interrupted`, and the caller exits with `128 + signal`. Nothing here leaves
an orphan process behind a signal.
"""

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass

from vacation_memories import exit_codes

HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
TERMINATE_GRACE_SECONDS = 2.0
POLL_SECONDS = 0.2
STDERR_TAIL_CHARS = 2000


class Interrupted(Exception):
    """A shutdown signal arrived; the exit code is `128 + signal`."""

    def __init__(self, signum: int):
        super().__init__(f"interrupted by {signal.Signals(signum).name}")
        self.signum = signum

    @property
    def exit_code(self) -> int:
        return exit_codes.SIGNAL_BASE + self.signum


class ToolError(Exception):
    """A tool exited with an error (or could not start)."""

    def __init__(self, command: list[str], returncode: int | None, stderr: str):
        tail = stderr.strip()[-STDERR_TAIL_CHARS:]
        super().__init__(f"{os.path.basename(command[0])} failed (exit {returncode}): {tail}")
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class ToolResult:
    returncode: int
    stdout: bytes
    stderr: str


class ShutdownController:
    """Signal handlers plus the registry of running children.

    Used as a context manager it installs its handlers (only from the main thread) and restores the
    previous ones on exit. Without installing anything it still works as a registry, which is how
    the tests drive it: `request(signal.SIGTERM)` behaves like the signal arriving.
    """

    def __init__(self, signals: tuple[int, ...] = HANDLED_SIGNALS, grace: float = TERMINATE_GRACE_SECONDS):
        self.signals = signals
        self.grace = grace
        self.signum: int | None = None
        self._children: set[subprocess.Popen] = set()
        self._previous: dict[int, object] = {}

    @property
    def requested(self) -> bool:
        return self.signum is not None

    def check(self) -> None:
        if self.signum is not None:
            raise Interrupted(self.signum)

    def request(self, signum: int) -> None:
        """Record the first signal and stop every child."""
        if self.signum is None:
            self.signum = signum
        for child in list(self._children):
            _terminate_group(child, self.grace)

    def register(self, child: subprocess.Popen) -> None:
        self._children.add(child)
        if self.signum is not None:  # the signal arrived between the check and the start
            _terminate_group(child, self.grace)

    def unregister(self, child: subprocess.Popen) -> None:
        self._children.discard(child)

    def __enter__(self) -> "ShutdownController":
        if threading.current_thread() is threading.main_thread():
            for signum in self.signals:
                self._previous[signum] = signal.signal(signum, lambda number, _frame: self.request(number))
        return self

    def __exit__(self, *exc_info) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous.clear()


def _terminate_group(child: subprocess.Popen, grace: float) -> None:
    """SIGTERM to the child's process group, SIGKILL if it is still there after `grace` seconds."""
    if child.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if child.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()


class ToolRunner:
    """Runs FFmpeg and FFprobe. `controller` makes the runs interruptible."""

    def __init__(self, controller: ShutdownController | None = None):
        self.controller = controller or ShutdownController()

    def ffmpeg(self, args: list[str], *, timeout: float | None = None) -> ToolResult:
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "error", "-y"]
        return self.run(command + args, timeout=timeout)

    def ffprobe(self, args: list[str], *, timeout: float | None = None) -> ToolResult:
        # ffprobe has no -nostdin option; it never reads stdin, and stdin is /dev/null anyway.
        return self.run(["ffprobe", "-hide_banner", "-loglevel", "error", *args], timeout=timeout)

    def run(self, command: list[str], *, timeout: float | None = None) -> ToolResult:
        """Run to completion; raise `ToolError` on a non-zero exit, `Interrupted` on a signal."""
        self.controller.check()
        try:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise ToolError(command, None, f"cannot start: {error.strerror or error}") from error
        self.controller.register(child)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                try:
                    stdout, stderr = child.communicate(timeout=POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if self.controller.requested:
                        _terminate_group(child, self.controller.grace)
                        self.controller.check()
                    if deadline is not None and time.monotonic() > deadline:
                        _terminate_group(child, self.controller.grace)
                        raise ToolError(command, None, f"timed out after {timeout:g} s") from None
        except BaseException:
            _terminate_group(child, self.controller.grace)
            raise
        finally:
            self.controller.unregister(child)
        self.controller.check()
        text = stderr.decode("utf-8", errors="replace")
        if child.returncode != 0:
            raise ToolError(command, child.returncode, text)
        return ToolResult(child.returncode, stdout, text)


def parallel_map(function: Callable, items: list, jobs: int, controller: ShutdownController) -> list:
    """`[function(item) for item in items]` on `jobs` threads, in order, stopping at a signal.

    The work happens in subprocesses, so threads are enough. The first exception (an interruption
    included) is raised after the other running calls have finished or been stopped.
    """
    if jobs <= 1 or len(items) <= 1:
        results = []
        for item in items:
            controller.check()
            results.append(function(item))
        return results

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(function, item) for item in items]
        pending = set(futures)
        try:
            while pending:
                done, pending = wait(pending, timeout=POLL_SECONDS, return_when=FIRST_EXCEPTION)
                failed = next((future for future in done if future.exception() is not None), None)
                if failed is not None or controller.requested:
                    for future in pending:
                        future.cancel()
                    break
        finally:
            for future in futures:
                future.cancel()
    controller.check()
    for future in futures:
        if not future.cancelled() and future.exception() is not None:
            raise future.exception()
    return [future.result() for future in futures]
