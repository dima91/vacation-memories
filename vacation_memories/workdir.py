"""Working folder: private permissions, temporary files inside it, single-instance lock, status file."""

import fcntl
import json
import logging
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vacation_memories.atomic import atomic_write_json

logger = logging.getLogger(__name__)

PRIVATE_MODE = 0o700
STATES = ("running", "done", "failed", "interrupted")
LOCK_NAME = ".lock"
STATUS_NAME = "status.json"
HOLDER_WAIT_SECONDS = 1.0
HOLDER_POLL_SECONDS = 0.05
STATUS_MAX_AGE_SECONDS = 60


@dataclass(frozen=True)
class WorkdirPaths:
    root: Path
    tmp: Path


class WorkdirInUseError(Exception):
    """Another instance holds the lock of the working folder (exit code 5)."""


def prepare_workdir(workdir: Path) -> WorkdirPaths:
    """Create the workdir and `<workdir>/tmp` (mode 0700) and make the process use that `tmp`.

    On many servers `/tmp` is a small in-RAM tmpfs that FFmpeg would fill.
    """
    root = Path(workdir).absolute()
    tmp = root / "tmp"
    for folder in (root, tmp):
        folder.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
        _restrict(folder)
    os.environ["TMPDIR"] = str(tmp)
    tempfile.tempdir = str(tmp)
    return WorkdirPaths(root, tmp)


def _restrict(folder: Path) -> None:
    if folder.stat().st_mode & 0o777 == PRIVATE_MODE:
        return
    try:
        folder.chmod(PRIVATE_MODE)
    except PermissionError:
        logger.warning("cannot restrict the permissions of %s to 0700", folder)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StatusWriter:
    """`status.json`: who runs, in which phase, how far along. Every update is atomic."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {}

    def start(self) -> None:
        now = _now()
        self._data = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "state": "running",
            "phase": None,
            "done": 0,
            "total": None,
            "eta_seconds": None,
            "started_at": now,
            "updated_at": now,
        }
        self._write()

    def update(self, **fields: object) -> None:
        self._data.update(fields)
        self._write()

    def finish(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state: {state}")
        self.update(state=state)

    def read(self) -> dict:
        return read_status(self.path.parent) or {}

    def _write(self) -> None:
        self._data["updated_at"] = _now()
        atomic_write_json(self.path, self._data)


def read_status(workdir: Path) -> dict | None:
    """The workdir's `status.json`, or None if missing or unreadable."""
    try:
        data = json.loads((Path(workdir) / STATUS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_current(status: dict | None) -> bool:
    """A running instance that updated its status recently (it does so every few seconds)."""
    if not status or status.get("state") != "running" or not {"hostname", "pid", "started_at"} <= status.keys():
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(status["updated_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return age.total_seconds() <= STATUS_MAX_AGE_SECONDS


class WorkdirLock:
    """`flock` on `<workdir>/.lock`: the kernel releases it when the process dies.

    Every `docker compose run` is a different container, so a refused instance is told the
    hostname as well as the PID and start time of the active one (from `status.json`).
    """

    def __init__(self, workdir: Path, holder_wait: float = HOLDER_WAIT_SECONDS):
        self.workdir = Path(workdir)
        self.status = StatusWriter(self.workdir / STATUS_NAME)
        self.holder_wait = holder_wait
        self._descriptor: int | None = None

    def acquire(self) -> None:
        descriptor = os.open(self.workdir / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise WorkdirInUseError(self._describe_holder()) from None
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        try:
            self.status.start()
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)  # closing the descriptor drops the flock
            self._descriptor = None

    def _describe_holder(self) -> str:
        """Name the active instance from `status.json`, but only from a current one.

        The holder writes its status just after taking the lock, so for a moment the file may
        still be the one left by an earlier run: wait briefly for a fresh status rather than
        blame the wrong host and PID, and fall back to a generic message.
        """
        deadline = time.monotonic() + self.holder_wait
        while True:
            status = read_status(self.workdir)
            if _is_current(status):
                return (
                    f"the workdir {self.workdir} is in use by another instance "
                    f"(hostname {status['hostname']}, PID {status['pid']}, started at {status['started_at']})"
                )
            if time.monotonic() >= deadline:
                return f"the workdir {self.workdir} is in use by another instance"
            time.sleep(HOLDER_POLL_SECONDS)

    def __enter__(self) -> "WorkdirLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
