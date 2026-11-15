"""Jobs: child processes of the existing CLI (section 6 of `UI-SPEC.md`).

A job is `python -m vacation_memories …` run on the project's workdir: it takes the workdir lock, writes
`status.json` and its log like any run of the CLI. The server never takes the lock and only reads the
status. One job at a time in the whole editor: the machine is shared with Ollama.
"""

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from vacation_memories.core import exit_codes
from vacation_memories.core.atomic import atomic_write_json
from vacation_memories.core.workdir import read_status
from vacation_memories.editor.errors import ApiError, conflict

logger = logging.getLogger(__name__)

PLAN, PREVIEW, EXPORT = "plan", "preview", "export"
JOB_TYPES = (PLAN, PREVIEW, EXPORT)
JOB_FILE = "job.json"
OUTPUT_FILE = "job_output.log"
GRACE_SECONDS = 4.0  # the server must stop within 5 s of SIGTERM (section 4)
OUTPUT_TAIL_BYTES = 4000
STATUS_KEYS = ("state", "phase", "done", "total", "eta_seconds", "started_at", "updated_at")
INTERRUPTED = frozenset({exit_codes.SIGNAL_BASE + signal.SIGHUP, exit_codes.SIGNAL_BASE + signal.SIGINT,
                         exit_codes.SIGNAL_BASE + signal.SIGTERM})

# Exit codes turned into messages (section 6): a stable key for the interface and an English text.
_EXIT_MESSAGES = {
    exit_codes.OK: ("done", "the job finished"),
    exit_codes.ERROR: ("failed", "the job failed; see the log of the project"),
    exit_codes.USAGE: ("invalid_input", "the settings or the plan are not valid"),
    exit_codes.VERIFICATION_FAILED: ("check_failed", "the final check of the video failed; the partial file is kept"),
    exit_codes.REQUIREMENTS: ("missing_requirements", "something the job needs is missing (FFmpeg, free space, or "
                                                      "Ollama and its model for the analysis with AI)"),
    exit_codes.WORKDIR_IN_USE: ("project_busy", "another job is using this project"),
}


def exit_message(code: int | None, detail: str | None = None) -> dict | None:
    """What an exit code means for the user; `detail` is the message the CLI printed, when it printed one."""
    if code is None:
        return None
    if code in INTERRUPTED:
        key, text = "interrupted", "the job was stopped; run it again to continue"
    else:
        key, text = _EXIT_MESSAGES.get(code, ("failed", f"the job failed (exit code {code})"))
    return {"code": code, "key": key, "message": text, "detail": detail if code != exit_codes.OK else None}


@dataclass
class Job:
    id: str
    project: str
    type: str
    workdir: Path
    name: str | None
    process: subprocess.Popen | None = None
    started_at: str = ""
    finished_at: str | None = None
    exit_code: int | None = None
    cancelled: bool = False
    plan_revision: str | None = None  # the version of the plan a render started from (section 6)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def running(self) -> bool:
        return self.exit_code is None

    @property
    def state(self) -> str:
        if self.exit_code is None:
            return "stopping" if self.cancelled else "running"
        if self.exit_code == exit_codes.OK:
            return "done"
        return "interrupted" if self.exit_code in INTERRUPTED else "failed"

    def to_dict(self) -> dict:
        detail = _error_line(self.workdir) if self.exit_code not in (None, exit_codes.OK) else None
        return {"id": self.id, "type": self.type, "name": self.name, "state": self.state,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "exit": exit_message(self.exit_code, detail), "plan_revision": self.plan_revision}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _error_line(workdir: Path) -> str | None:
    """The last `error:` line the CLI printed (the reason of a code 2, 3 or 4), if any."""
    try:
        with open(Path(workdir) / OUTPUT_FILE, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - OUTPUT_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line[len("error:"):].strip() for line in tail.splitlines() if line.startswith("error:")]
    return lines[-1] if lines else None


class JobManager:
    def __init__(self, command: Sequence[str] | None = None, env: Mapping[str, str] | None = None,
                 grace: float = GRACE_SECONDS):
        self.command = list(command) if command else [sys.executable, "-m", "vacation_memories"]
        self.env = None if env is None else dict(env)
        self.grace = grace
        self._lock = threading.Lock()
        self._job: Job | None = None

    # --- starting and stopping ---

    def start(self, project: str, workdir: Path, job_type: str, arguments: Sequence[str], *,
              name: str | None = None, plan_revision: str | None = None) -> Job:
        """Start a job, unless one is running anywhere in the editor (409)."""
        with self._lock:
            if self._job is not None and self._job.running:
                raise conflict("job_running", "another job is already running", job=self._job.id)
            job = Job(uuid.uuid4().hex[:12], project, job_type, Path(workdir), name, started_at=_now(),
                      plan_revision=plan_revision)
            try:
                output = open(Path(workdir) / OUTPUT_FILE, "wb")
            except OSError as error:
                raise ApiError(500, "cannot_start", f"cannot prepare the job: {error.strerror}") from None
            try:
                job.process = subprocess.Popen(
                    [*self.command, *arguments], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True, env=self.env,
                )
            except OSError as error:
                raise ApiError(500, "cannot_start", f"cannot start the job: {error.strerror}") from None
            finally:
                output.close()
            self._job = job
            self._record(job)
        threading.Thread(target=self._watch, args=(job,), daemon=True, name=f"job-{job.id}").start()
        logger.info("job %s (%s) started on project %s", job.id, job_type, project)
        return job

    def cancel(self, project: str) -> bool:
        """Orderly stop (SIGTERM to the process group, exit code 143). False if there is nothing to stop."""
        with self._lock:
            job = self._job
            if job is None or not job.running or job.project != project:
                return False
            job.cancelled = True
            self._signal(job, signal.SIGTERM)
        return True

    def stop_all(self) -> None:
        """At the server's shutdown: stop the running job, and kill it if it outlives the grace period."""
        with self._lock:
            job = self._job
            if job is None or not job.running:
                return
            job.cancelled = True
            self._signal(job, signal.SIGTERM)
        if not job._done.wait(self.grace):
            with self._lock:
                self._signal(job, signal.SIGKILL)
            job._done.wait(2.0)

    def wait(self, job: Job, timeout: float | None = None) -> bool:
        return job._done.wait(timeout)

    @staticmethod
    def _signal(job: Job, signum: int) -> None:
        if job.process is not None and job.process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(job.process.pid, signum)

    def _watch(self, job: Job) -> None:
        code = job.process.wait()
        if code < 0:  # killed by a signal the child did not handle: the shell's convention
            code = exit_codes.SIGNAL_BASE - code
        with self._lock:
            job.exit_code = code
            job.finished_at = _now()
            self._record(job)
        job._done.set()
        logger.info("job %s (%s) ended with code %d", job.id, job.type, code)

    def _record(self, job: Job) -> None:
        try:
            atomic_write_json(job.workdir / JOB_FILE, {
                "id": job.id, "type": job.type, "name": job.name, "started_at": job.started_at,
                "finished_at": job.finished_at, "exit_code": job.exit_code, "cancelled": job.cancelled,
                "plan_revision": job.plan_revision,
            })
        except OSError as error:
            logger.warning("cannot record the job: %s", error.strerror or error)

    # --- what the interface asks ---

    def running_project(self) -> str | None:
        with self._lock:
            return self._job.project if self._job is not None and self._job.running else None

    def running_type(self, project: str) -> str | None:
        """The type of the job running on `project`, if one is."""
        with self._lock:
            job = self._job
            return job.type if job is not None and job.running and job.project == project else None

    def current(self, project: str, workdir: Path) -> dict | None:
        """The last job of a project: the one in memory, or the one `job.json` remembers after a restart."""
        with self._lock:
            job = self._job
            if job is not None and job.project == project:
                return job.to_dict()
        try:
            stored = json.loads((Path(workdir) / JOB_FILE).read_text(encoding="utf-8"))
            code = stored["exit_code"]
            if code is None:  # the editor stopped while it ran: whatever the child did, it is not running now
                code = exit_codes.SIGNAL_BASE + signal.SIGTERM
            return {"id": stored["id"], "type": stored["type"], "name": stored.get("name"),
                    "state": Job(stored["id"], project, stored["type"], Path(workdir), None, exit_code=code).state,
                    "started_at": stored["started_at"], "finished_at": stored.get("finished_at"),
                    "exit": exit_message(code, _error_line(workdir) if code != exit_codes.OK else None),
                    "plan_revision": stored.get("plan_revision")}
        except (OSError, ValueError, KeyError, TypeError):
            return None


def progress(workdir: Path) -> dict | None:
    """The part of the child's `status.json` the interface shows (no host name or PID)."""
    status = read_status(workdir)
    return None if status is None else {key: status.get(key) for key in STATUS_KEYS}


# --- the command lines of the jobs (section 6) -------------------------------------------------------------


def _common(input_dir: Path, workdir: Path, settings: dict) -> list[str]:
    arguments = ["--input", str(input_dir), "--workdir", str(workdir), "--jobs", str(settings["jobs"]),
                 "--hwaccel", str(settings["hwaccel"])]
    if settings["nice"]:
        arguments += ["--nice", str(settings["nice"])]
    return arguments


def plan_arguments(input_dir: Path, workdir: Path, settings: dict, *, ai: bool) -> list[str]:
    """Analysis and plan: `--dry-run` with the project's options."""
    arguments = ["--dry-run", *_common(input_dir, workdir, settings), "--duration", str(settings["duration"]),
                 "--structure", settings["structure"], "--aspect", settings["aspect"],
                 "--resolution", settings["resolution"]]
    if settings["assume_tz"]:
        arguments += ["--assume-tz", settings["assume_tz"]]
    if not ai:
        return [*arguments, "--no-ai"]
    arguments.append("--require-ai")  # a result without AI is never produced silently
    if settings["ai_frames"] is not None:
        return [*arguments, "--ai-frames", str(settings["ai_frames"])]
    return [*arguments, "--ai-time-budget", str(settings["ai_time_budget"])]


def render_arguments(input_dir: Path, workdir: Path, settings: dict, *, name: str, preview: bool) -> list[str]:
    """Preview or export: `--render-only` on the plan of the project (it never uses the AI, so `--no-ai`)."""
    arguments = ["--render-only", "--no-ai", *_common(input_dir, workdir, settings), "--output", f"{name}.mp4",
                 "--aspect", settings["aspect"], "--resolution", settings["resolution"]]
    if preview:
        arguments.append("--preview")
    if settings["music"]:
        arguments += ["--music", settings["music"][0]]
    return arguments
