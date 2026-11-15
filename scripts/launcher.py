#!/usr/bin/env python3
"""Single-file launcher of Vacation Memories (step U8 of UI-SPEC.md).

It runs on the host, with the standard library only (PyInstaller bundles the interpreter, `scripts/build_launcher.py`
builds the executable). It extracts the compose files it carries, asks the few environment variables on the first
start in a small page shown in a Google Chrome `--app` window, pulls the published image, starts `editor` and
`nginx`, and sends the same window to the editor. When the window is closed it runs `docker compose stop`.

The server never opens a browser and the containers do not depend on a display: only this host-side program does.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import fcntl
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

APP_NAME = "vacation-memories"
PROJECT_NAME = "vacation-memories-app"
WINDOW_TITLE = "Vacation Memories"
EDITOR_URL = "http://127.0.0.1:8080"
HEALTH_URL = EDITOR_URL + "/api/health"
DIST_CONFIG = "dist_config.json"
BROWSER_CANDIDATES = ("google-chrome", "google-chrome-stable")
COMPOSE_FILE = "compose.yaml"
INTEL_GPU_FILE = "compose.intel-gpu.yaml"
SELINUX_FILE = "compose.selinux.yaml"
PAYLOAD_FILES = (
    COMPOSE_FILE,
    INTEL_GPU_FILE,
    SELINUX_FILE,
    "docker/nginx.conf",
    "docker/compose.toml",
)
UP_WAIT_SECONDS = 240
HEALTH_WAIT_SECONDS = 30
MAX_BODY_BYTES = 8192
MAX_LOG_LINES = 12
MAX_LINE_CHARS = 300

log = logging.getLogger(APP_NAME)


class StartError(Exception):
    """A failure of the start sequence, with a message the person can act on."""


# --- directories and files carried by the executable ---


@dataclass(frozen=True)
class Layout:
    config_dir: Path
    data_root: Path
    version: str

    @property
    def data_dir(self) -> Path:
        return self.data_root / re.sub(r"[^A-Za-z0-9._-]", "_", self.version)

    @property
    def answers_file(self) -> Path:
        return self.config_dir / "answers.json"

    @property
    def env_file(self) -> Path:
        return self.config_dir / "compose.env"

    @property
    def log_file(self) -> Path:
        return self.config_dir / "launcher.log"

    @property
    def lock_file(self) -> Path:
        return self.config_dir / "launcher.lock"

    @property
    def profile_dir(self) -> Path:
        return self.config_dir / "profile"

    @property
    def default_music_dir(self) -> Path:
        return self.data_root / "music"


def default_layout(version: str, environ=os.environ) -> Layout:
    home = Path(environ.get("HOME") or Path.home())
    config_home = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    data_home = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return Layout(config_home / APP_NAME, data_home / APP_NAME, version)


@dataclass(frozen=True)
class DistConfig:
    image: Optional[str]
    version: str

    @property
    def runtime_image(self) -> Optional[str]:
        return f"{self.image}:{self.version}" if self.image else None


def payload_root() -> Path:
    """Where the compose files are: the bundle of the executable, or the repository when run as a script."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "payload"
    return Path(__file__).resolve().parent.parent


def load_dist_config(root: Path) -> DistConfig:
    try:
        data = json.loads((root / DIST_CONFIG).read_text(encoding="utf-8"))
        image = data.get("image") or None
        version = str(data.get("version") or "dev")
    except (OSError, ValueError, AttributeError):
        return DistConfig(None, "dev")
    return DistConfig(image if isinstance(image, str) else None, version)


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def extract_payload(source: Path, layout: Layout) -> List[str]:
    """Copy the compose files out of the bundle: bind mounts need paths that outlive this process."""
    changed = []
    for name in PAYLOAD_FILES:
        origin = source / name
        if not origin.is_file():
            raise StartError(f"The launcher is incomplete: {name} is missing.")
        content = origin.read_bytes()
        target = layout.data_dir / name
        if target.is_file() and target.read_bytes() == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, target)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()
        changed.append(name)
    return changed


def self_check(root: Path) -> int:
    missing = [name for name in PAYLOAD_FILES if not (root / name).is_file()]
    for name in missing:
        print(f"missing: {name}", file=sys.stderr)
    dist = load_dist_config(root)
    print(f"{APP_NAME} {dist.version}, image {dist.runtime_image or 'local build'}, payload at {root}")
    return 1 if missing else 0


# --- the answers of the person ---


@dataclass
class Answers:
    media_dir: str = ""
    music_dir: str = ""
    work_dir: str = ""
    assume_tz: str = ""
    ollama_model: str = ""
    intel_gpu: bool = False
    selinux: bool = False


UNSAFE_TEXT = re.compile(r"['\x00-\x1f]")
TIMEZONE = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}$")
MODEL_NAME = re.compile(r"^[A-Za-z0-9._:/\-]{1,128}$")


def default_answers(environ=os.environ) -> Answers:
    home = Path(environ.get("HOME") or Path.home())
    timezone = ""
    with contextlib.suppress(OSError):
        timezone = Path("/etc/timezone").read_text(encoding="utf-8").strip()
    selinux = False
    with contextlib.suppress(OSError):
        selinux = Path("/sys/fs/selinux/enforce").read_text(encoding="utf-8").strip() == "1"
    return Answers(
        work_dir=str(home / "VacationMemories"),
        assume_tz=timezone if TIMEZONE.match(timezone) else "",
        intel_gpu=Path("/dev/dri").exists(),
        selinux=selinux,
    )


def _existing_ancestor(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def validate_answers(raw: dict, layout: Layout) -> Tuple[Answers, Dict[str, str]]:
    """Return the cleaned answers and the error of every field that cannot be used."""
    errors: Dict[str, str] = {}

    def text(key: str) -> str:
        value = raw.get(key, "")
        return value.strip() if isinstance(value, str) else ""

    def flag(key: str) -> bool:
        return raw.get(key) is True

    def folder(key: str, must_exist: bool, writable: bool) -> str:
        value = text(key)
        if not value:
            return ""
        if UNSAFE_TEXT.search(value):
            errors[key] = "The path has a character that is not supported (a quote or a control character)."
            return value
        path = Path(value).expanduser()
        if not path.is_absolute():
            errors[key] = "Use an absolute path, for example /home/you/Photos."
            return value
        wanted = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
        if path.exists():
            if not path.is_dir():
                errors[key] = "This is not a folder."
            elif not os.access(path, wanted):
                errors[key] = "This folder cannot be read." if not writable else "This folder cannot be written."
        elif must_exist:
            errors[key] = "This folder does not exist."
        elif not os.access(_existing_ancestor(path), os.W_OK | os.X_OK):
            errors[key] = "This folder cannot be created here."
        return str(path)

    media = folder("media_dir", must_exist=True, writable=False)
    if not media and "media_dir" not in errors:
        errors["media_dir"] = "Choose the folder that holds the vacations."
    music = folder("music_dir", must_exist=True, writable=False)
    work = folder("work_dir", must_exist=False, writable=True)
    if not work and "work_dir" not in errors:
        errors["work_dir"] = "Choose a folder for the projects and the videos."

    timezone = text("assume_tz")
    if timezone and not TIMEZONE.match(timezone):
        errors["assume_tz"] = "Use a name such as Europe/Rome."
    model = text("ollama_model")
    if model and not MODEL_NAME.match(model):
        errors["ollama_model"] = "This is not a valid model name."

    answers = Answers(media, music, work, timezone, model, flag("intel_gpu"), flag("selinux"))
    return answers, errors


def load_answers(layout: Layout) -> Optional[dict]:
    try:
        data = json.loads(layout.answers_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_answers(layout: Layout, answers: Answers) -> None:
    atomic_write(layout.answers_file, json.dumps(asdict(answers), indent=2) + "\n", 0o600)


def effective_music_dir(answers: Answers, layout: Layout) -> str:
    return answers.music_dir or str(layout.default_music_dir)


def prepare_directories(answers: Answers, layout: Layout) -> None:
    """Compose binds the folders with `create_host_path: false`, so they must exist."""
    Path(answers.work_dir).mkdir(parents=True, exist_ok=True)
    if not answers.music_dir:
        layout.default_music_dir.mkdir(parents=True, exist_ok=True)


def compose_files(answers: Answers) -> List[str]:
    files = [COMPOSE_FILE]
    if answers.intel_gpu:
        files.append(INTEL_GPU_FILE)
    if answers.selinux:
        files.append(SELINUX_FILE)
    return files


def build_env(answers: Answers, layout: Layout, dist: DistConfig, uid: int, gid: int) -> Dict[str, str]:
    env = {
        "MEDIA_DIR": answers.media_dir,
        "MUSIC_DIR": effective_music_dir(answers, layout),
        "WORK_DIR": answers.work_dir,
        "HOST_UID": str(uid),
        "HOST_GID": str(gid),
        "APP_VERSION": dist.version,
    }
    if answers.assume_tz:
        env["ASSUME_TZ"] = answers.assume_tz
    if answers.ollama_model:
        env["OLLAMA_MODEL"] = answers.ollama_model
    if dist.runtime_image:
        env["VM_IMAGE"] = dist.runtime_image
    return env


def render_env(env: Dict[str, str]) -> str:
    """Single quotes: compose reads them literally, with no `$` interpolation (the values have no quote)."""
    return "".join(f"{name}='{value}'\n" for name, value in env.items())


# --- running the Docker CLI ---


class Runner:
    """Runs commands, streams their output line by line and can terminate whatever is still running."""

    def __init__(self, popen=subprocess.Popen):
        self._popen = popen
        self._lock = threading.Lock()
        self._children: List = []

    def run(self, command: List[str], on_line: Optional[Callable[[str], None]] = None) -> Tuple[int, List[str]]:
        try:
            process = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        except FileNotFoundError:
            return 127, [f"{command[0]}: command not found"]
        except OSError as error:
            return 126, [f"{command[0]}: {error}"]
        with self._lock:
            self._children.append(process)
        lines: List[str] = []
        try:
            for raw in process.stdout:
                line = raw.rstrip()[:MAX_LINE_CHARS]
                if line:
                    lines.append(line)
                    if on_line:
                        on_line(line)
            return process.wait(), lines
        finally:
            with self._lock:
                if process in self._children:
                    self._children.remove(process)

    def cancel(self) -> None:
        with self._lock:
            children = list(self._children)
        for process in children:
            with contextlib.suppress(OSError):
                process.terminate()


class Compose:
    def __init__(self, runner: Runner, layout: Layout, files: List[str]):
        self._runner = runner
        self._layout = layout
        self._files = files

    def command(self, *arguments: str) -> List[str]:
        command = [
            "docker", "compose",
            "--project-name", PROJECT_NAME,
            "--project-directory", str(self._layout.data_dir),
            "--env-file", str(self._layout.env_file),
        ]
        for name in self._files:
            command += ["-f", str(self._layout.data_dir / name)]
        return command + list(arguments)

    def run(self, *arguments: str, on_line=None) -> Tuple[int, List[str]]:
        return self._runner.run(self.command(*arguments), on_line)


def check_docker(runner: Runner) -> Optional[str]:
    code, lines = runner.run(["docker", "compose", "version"])
    if code == 127:
        return "Docker is not installed: the 'docker' command was not found."
    if code != 0:
        return "The Docker Compose plugin is not available. " + (lines[-1] if lines else "")
    code, lines = runner.run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if code != 0:
        if "permission denied" in " ".join(lines).lower():
            return "This user cannot use Docker. Add it to the 'docker' group, then log in again."
        return "The Docker daemon is not running or cannot be reached."
    return None


# --- state shown by the page ---


class Session:
    """What the page shows: the phase, a message and the last lines of the commands' output."""

    def __init__(self):
        self._lock = threading.Lock()
        self.phase = "working"
        self.message = ""
        self.error = ""
        self.errors: Dict[str, str] = {}
        self.lines: collections.deque = collections.deque(maxlen=MAX_LOG_LINES)

    def update(self, phase: Optional[str] = None, message: Optional[str] = None, error: Optional[str] = None,
               errors: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            if phase is not None:
                self.phase = phase
            if message is not None:
                self.message = message
            if error is not None:
                self.error = error
            if errors is not None:
                self.errors = errors

    def add_line(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self.phase,
                "message": self.message,
                "error": self.error,
                "errors": dict(self.errors),
                "lines": list(self.lines),
            }


class Launcher:
    def __init__(self, layout: Layout, dist: DistConfig, source: Path, *, runner: Optional[Runner] = None,
                 urlopen=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic,
                 uid: Optional[int] = None, gid: Optional[int] = None, environ=os.environ):
        self.layout = layout
        self.dist = dist
        self.source = source
        self.runner = runner or Runner()
        self.session = Session()
        self.answers = default_answers(environ)
        self.stack_started = False
        self._urlopen = urlopen
        self._sleep = sleep
        self._clock = clock
        self._uid = os.getuid() if uid is None else uid
        self._gid = os.getgid() if gid is None else gid
        self._stopping = threading.Event()
        self._busy = threading.Lock()
        self._threads: List[threading.Thread] = []

    # entry points used at launch and by the page

    def begin(self, reconfigure: bool) -> None:
        saved = load_answers(self.layout)
        if saved is not None:
            answers, errors = validate_answers(saved, self.layout)
            self.answers = answers
            if not errors and not reconfigure:
                self.start()
                return
            self.session.update("setup", "", "", errors if not reconfigure else {})
            return
        self.session.update("setup", "", "", {})

    def submit_setup(self, raw: dict) -> Dict[str, str]:
        answers, errors = validate_answers(raw, self.layout)
        self.answers = answers
        if errors:
            self.session.update(errors=errors)
            return errors
        save_answers(self.layout, answers)
        self.start()
        return {}

    def reconfigure(self) -> None:
        if self.session.snapshot()["phase"] in ("setup", "error"):
            self.session.update("setup", "", "", {})

    def retry(self) -> None:
        if self.session.snapshot()["phase"] == "error":
            self.start()

    def start(self) -> None:
        self.session.update("working", "Checking Docker", "", {})
        thread = threading.Thread(target=self._start_guarded, name="start", daemon=True)
        self._threads.append(thread)
        thread.start()

    # the start sequence

    def _start_guarded(self) -> None:
        if not self._busy.acquire(blocking=False):
            return
        failure = None
        try:
            self._start_stack()
        except StartError as error:
            log.error("start failed: %s", error)
            failure = None if self._stopping.is_set() else str(error)
        except Exception as error:  # a bug must reach the person as a message, not as a window that never changes
            log.exception("unexpected failure")
            failure = f"Unexpected error: {error}"
        finally:
            self._busy.release()
        if failure:
            self.session.update("error", error=failure)
            notify(failure)
        elif not self._stopping.is_set():
            self.session.update("ready", "")

    def _check_stopping(self) -> None:
        if self._stopping.is_set():
            raise StartError("The launcher is closing.")

    def _start_stack(self) -> None:
        problem = check_docker(self.runner)
        if problem:
            raise StartError(problem)
        self._check_stopping()

        extract_payload(self.source, self.layout)
        prepare_directories(self.answers, self.layout)
        env = build_env(self.answers, self.layout, self.dist, self._uid, self._gid)
        atomic_write(self.layout.env_file, render_env(env), 0o600)
        compose = Compose(self.runner, self.layout, compose_files(self.answers))

        def show(line: str) -> None:
            log.info("%s", line)
            self.session.add_line(line)

        image = self.dist.runtime_image
        if image and self.runner.run(["docker", "image", "inspect", image])[0] != 0:
            self.session.update(message="Downloading the application (first start only, this can take minutes)")
            code, _ = compose.run("pull", "editor", on_line=show)
            self._check_stopping()
            if code != 0:
                raise StartError(f"The image {image} could not be downloaded. Check the network and try again.")

        self.session.update(message="Starting the editor")
        self.stack_started = True
        code, _ = compose.run(
            "up", "-d", "--no-build", "--wait", "--wait-timeout", str(UP_WAIT_SECONDS), "editor", "nginx",
            on_line=show,
        )
        self._check_stopping()
        if code != 0:
            raise StartError("The editor did not start. The last lines of the output are below.")

        self._start_ollama(compose)
        self._wait_for_health()

    def _start_ollama(self, compose: Compose) -> None:
        def work() -> None:
            code, lines = compose.run("up", "-d", "ollama", "ollama-pull")
            if code != 0:
                log.warning("Ollama did not start (the editor works without it): %s", " | ".join(lines[-3:]))

        thread = threading.Thread(target=work, name="ollama", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _wait_for_health(self) -> None:
        deadline = self._clock() + HEALTH_WAIT_SECONDS
        while True:
            self._check_stopping()
            try:
                with self._urlopen(HEALTH_URL, timeout=3):
                    return
            except (urllib.error.URLError, OSError):
                pass
            if self._clock() >= deadline:
                raise StartError("The editor does not answer at " + EDITOR_URL + ".")
            self._sleep(1)

    # closing

    def shutdown(self) -> None:
        self._stopping.set()
        self.runner.cancel()
        if self.stack_started:
            compose = Compose(self.runner, self.layout, compose_files(self.answers))
            code, lines = compose.run("stop")
            if code != 0:
                log.warning("docker compose stop failed: %s", " | ".join(lines[-3:]))


# --- the page and its server ---


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vacation Memories</title>
<style nonce="__NONCE__">
:root { --bg: #f6f4ef; --card: #ffffff; --text: #1f2933; --muted: #5b6770; --line: #d9d4c7; --accent: #1f5f8b;
        --accent-text: #ffffff; --error: #a32424; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #16191c; --card: #20252a; --text: #e8eaec; --muted: #9aa5ad; --line: #38414a; --accent: #6bb0e0;
          --accent-text: #0e1418; --error: #ff8f8f; }
}
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--text);
       font: 16px/1.5 system-ui, sans-serif; padding: 16px; }
main { width: min(560px, 100%); background: var(--card); border: 1px solid var(--line); border-radius: 12px;
       padding: 28px; }
h1 { font-size: 22px; margin: 0 0 4px; }
p { margin: 0 0 16px; color: var(--muted); }
label { display: block; font-weight: 600; margin-top: 14px; }
.hint { color: var(--muted); font-size: 14px; margin: 2px 0 6px; font-weight: 400; }
input[type=text] { width: 100%; padding: 9px 10px; border: 1px solid var(--line); border-radius: 8px;
                   background: transparent; color: inherit; font: inherit; }
input[type=text]:focus-visible, button:focus-visible, summary:focus-visible { outline: 3px solid var(--accent);
                                                                               outline-offset: 2px; }
.check { display: flex; gap: 8px; align-items: center; font-weight: 400; margin-top: 10px; }
.field-error { color: var(--error); font-size: 14px; min-height: 0; }
details { margin-top: 18px; }
summary { cursor: pointer; font-weight: 600; }
button { margin-top: 22px; padding: 10px 18px; border: 0; border-radius: 8px; background: var(--accent);
         color: var(--accent-text); font: inherit; font-weight: 600; cursor: pointer; }
button.secondary { background: transparent; color: var(--text); border: 1px solid var(--line); margin-left: 8px; }
pre { margin: 16px 0 0; padding: 10px; background: var(--bg); border-radius: 8px; font-size: 13px;
      white-space: pre-wrap; word-break: break-word; max-height: 220px; overflow: auto; }
.error { color: var(--error); font-weight: 600; }
.spinner { width: 22px; height: 22px; border: 3px solid var(--line); border-top-color: var(--accent);
           border-radius: 50%; animation: spin 1s linear infinite; margin-bottom: 12px; }
@media (prefers-reduced-motion: reduce) { .spinner { animation: none; } }
@keyframes spin { to { transform: rotate(360deg); } }
[hidden] { display: none !important; }
</style>
</head>
<body>
<main>
  <section id="setup" hidden>
    <h1>Vacation Memories</h1>
    <p>Tell the editor where your files are. You can change these choices later from the launcher menu.</p>
    <form id="form" novalidate>
      <label for="media_dir">Folder with the vacations</label>
      <div class="hint">Photos and clips, one subfolder per vacation. It is only read, never changed.</div>
      <input type="text" id="media_dir" name="media_dir" autocomplete="off" placeholder="/home/you/Photos">
      <div class="field-error" id="error-media_dir" role="alert"></div>

      <label for="work_dir">Folder for the projects and the videos</label>
      <div class="hint">Analysis, plans, previews and the finished videos are written here.</div>
      <input type="text" id="work_dir" name="work_dir" autocomplete="off">
      <div class="field-error" id="error-work_dir" role="alert"></div>

      <label for="music_dir">Folder with the music (optional)</label>
      <div class="hint">Leave empty to start with an empty music folder.</div>
      <input type="text" id="music_dir" name="music_dir" autocomplete="off">
      <div class="field-error" id="error-music_dir" role="alert"></div>

      <details>
        <summary>Advanced</summary>
        <label for="assume_tz">Time zone for files without one</label>
        <input type="text" id="assume_tz" name="assume_tz" autocomplete="off" placeholder="Europe/Rome">
        <div class="field-error" id="error-assume_tz" role="alert"></div>
        <label for="ollama_model">Vision model (empty: the default)</label>
        <input type="text" id="ollama_model" name="ollama_model" autocomplete="off">
        <div class="field-error" id="error-ollama_model" role="alert"></div>
        <label class="check"><input type="checkbox" id="intel_gpu" name="intel_gpu"> Use the Intel integrated GPU for the model</label>
        <label class="check"><input type="checkbox" id="selinux" name="selinux"> This computer uses SELinux</label>
      </details>
      <button type="submit" id="save">Save and start</button>
    </form>
  </section>
  <section id="working" hidden>
    <div class="spinner" aria-hidden="true"></div>
    <h1 id="working-title">Vacation Memories</h1>
    <p id="working-message" role="status"></p>
    <pre id="working-log" hidden></pre>
  </section>
  <section id="failed" hidden>
    <h1>Vacation Memories</h1>
    <p class="error" id="failed-message" role="alert"></p>
    <pre id="failed-log" hidden></pre>
    <button type="button" id="retry">Try again</button>
    <button type="button" class="secondary" id="change">Change the folders</button>
  </section>
</main>
<script nonce="__NONCE__">
"use strict";
const TOKEN = "__TOKEN__";
const FIELDS = ["media_dir", "work_dir", "music_dir", "assume_tz", "ollama_model"];
const FLAGS = ["intel_gpu", "selinux"];
let phase = null;

function $(id) { return document.getElementById(id); }

async function api(path, body) {
  const options = { headers: { "X-Launcher-Token": TOKEN } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  return { status: response.status, data: await response.json() };
}

function show(name) {
  for (const section of document.querySelectorAll("main > section")) section.hidden = section.id !== name;
}

function showLog(element, lines) {
  element.hidden = lines.length === 0;
  element.textContent = lines.join("\\n");
  element.scrollTop = element.scrollHeight;
}

function showErrors(errors) {
  for (const name of FIELDS) $("error-" + name).textContent = errors[name] || "";
  const first = FIELDS.find((name) => errors[name]);
  if (first) $(first).focus();
}

function fillForm(answers, errors) {
  for (const name of FIELDS) $(name).value = answers[name] || "";
  for (const name of FLAGS) $(name).checked = !!answers[name];
  showErrors(errors);
}

async function tick() {
  try {
    const { data } = await api("/api/state");
    if (data.phase === "ready") {
      location.replace(data.editor_url);
      return;
    }
    if (data.phase !== phase) {
      phase = data.phase;
      if (phase === "setup") { show("setup"); fillForm(data.answers, data.errors); $("media_dir").focus(); }
      if (phase === "working") show("working");
      if (phase === "error") show("failed");
    }
    if (phase === "working") {
      $("working-message").textContent = data.message;
      showLog($("working-log"), data.lines);
    }
    if (phase === "error") {
      $("failed-message").textContent = data.error;
      showLog($("failed-log"), data.lines);
    }
  } catch (error) {
    // The launcher is closing: the window goes with it.
  }
  setTimeout(tick, 1000);
}

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = {};
  for (const name of FIELDS) body[name] = $(name).value;
  for (const name of FLAGS) body[name] = $(name).checked;
  const { status, data } = await api("/api/setup", body);
  if (status === 200) { phase = null; return; }
  showErrors(data.errors || {});
});
$("retry").addEventListener("click", async () => { await api("/api/retry", {}); phase = null; });
$("change").addEventListener("click", async () => { await api("/api/reconfigure", {}); phase = null; });
tick();
</script>
</body>
</html>
"""


def render_page(token: str, nonce: str) -> bytes:
    return PAGE.replace("__TOKEN__", token).replace("__NONCE__", nonce).encode("utf-8")


def make_handler(launcher: Launcher, token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "VacationMemoriesLauncher"

        def log_message(self, format, *arguments):  # noqa: A002 - signature of the base class
            log.debug("http %s", format % arguments)

        def _send(self, status: int, body: bytes, content_type: str, nonce: str = "") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if content_type.startswith("text/html"):
                self.send_header(
                    "Content-Security-Policy",
                    f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                    "connect-src 'self'; base-uri 'none'; form-action 'none'",
                )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _trusted(self, *, needs_token: bool, is_post: bool) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._json(403, {"error": "forbidden host"})
                return False
            if is_post and self.headers.get("Origin") not in allowed_origins:
                self._json(403, {"error": "forbidden origin"})
                return False
            if needs_token and not secrets.compare_digest(self.headers.get("X-Launcher-Token", ""), token):
                self._json(403, {"error": "forbidden"})
                return False
            return True

        def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            path = self.path.split("?", 1)[0]
            if not self._trusted(needs_token=path != "/", is_post=False):
                return
            if path == "/":
                nonce = secrets.token_urlsafe(12)
                self._send(200, render_page(token, nonce), "text/html; charset=utf-8", nonce)
            elif path == "/api/state":
                state = launcher.session.snapshot()
                state["answers"] = asdict(launcher.answers)
                state["editor_url"] = EDITOR_URL
                self._json(200, state)
            else:
                self._json(404, {"error": "not found"})

        def _read_json(self) -> Optional[dict]:
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._json(415, {"error": "JSON expected"})
                return None
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY_BYTES:
                self._json(413, {"error": "bad body size"})
                return None
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self._json(400, {"error": "JSON object expected"})
                return None
            return body

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if not self._trusted(needs_token=True, is_post=True):
                return
            body = self._read_json()
            if body is None:
                return
            phase = launcher.session.snapshot()["phase"]
            if path == "/api/setup":
                if phase not in ("setup", "error"):
                    self._json(409, {"error": "not accepting answers now"})
                    return
                errors = launcher.submit_setup(body)
                self._json(400 if errors else 200, {"errors": errors})
            elif path == "/api/retry":
                launcher.retry()
                self._json(200, {})
            elif path == "/api/reconfigure":
                launcher.reconfigure()
                self._json(200, {})
            else:
                self._json(404, {"error": "not found"})

    return Handler


class BootstrapServer:
    def __init__(self, launcher: Launcher):
        self.token = secrets.token_urlsafe(24)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.port = self._server.server_address[1]
        self._server.RequestHandlerClass = make_handler(launcher, self.token, self.port)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="bootstrap", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# --- window, notifications, desktop entry ---


def find_browser(preferred: Optional[str], which=None) -> Optional[str]:
    which = which or shutil.which
    for name in ([preferred] if preferred else list(BROWSER_CANDIDATES)):
        found = which(name)
        if found:
            return found
    return None


def browser_command(browser: str, url: str, profile_dir: Path) -> List[str]:
    """A dedicated profile makes Chrome start its own process, which ends with the window."""
    return [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--window-size=1440,900",
    ]


def notify(message: str, which=shutil.which, popen=subprocess.Popen) -> None:
    """A double click has no terminal: tell the person with a desktop notification when there is one."""
    tool = which("notify-send")
    if not tool:
        return
    with contextlib.suppress(OSError):
        popen([tool, WINDOW_TITLE, message], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL)


def quote_exec(argument: str) -> str:
    escaped = argument.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + escaped.replace("%", "%%") + '"'


def desktop_entry(command: List[str]) -> str:
    exec_line = " ".join(quote_exec(part) for part in command)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={WINDOW_TITLE}\n"
        "Comment=Create a memory video from the photos and clips of a vacation\n"
        f"Exec={exec_line}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Video;\n"
        "Actions=reconfigure;stop;\n"
        "\n"
        "[Desktop Action reconfigure]\n"
        "Name=Change folders and settings\n"
        f"Exec={exec_line} --reconfigure\n"
        "\n"
        "[Desktop Action stop]\n"
        "Name=Stop Vacation Memories\n"
        f"Exec={exec_line} --stop\n"
    )


def own_command() -> List[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def install_desktop(applications_dir: Path, command: List[str]) -> Path:
    target = applications_dir / f"{APP_NAME}.desktop"
    atomic_write(target, desktop_entry(command), 0o755)
    return target


# --- process lifecycle ---


@contextlib.contextmanager
def single_instance(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_file, "a+")  # not "w": a second instance must not wipe the pid of the first
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        yield False
        return
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    try:
        yield True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def wait_for_window(process, stop_event: threading.Event, poll: float = 0.5) -> None:
    """Block until Chrome exits; on a signal, close the window."""
    while process.poll() is None and not stop_event.wait(poll):
        pass
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def configure_logging(layout: Layout) -> None:
    layout.config_dir.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [
        logging.handlers.RotatingFileHandler(layout.log_file, maxBytes=200_000, backupCount=1, encoding="utf-8"),
    ]
    if sys.stderr and sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers,
                        force=True)


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Start Vacation Memories and open its window.")
    parser.add_argument("--reconfigure", action="store_true", help="ask the folders and settings again")
    parser.add_argument("--browser", help="Chrome command or path (default: google-chrome, then the default "
                                          "browser with xdg-open; or LAUNCHER_BROWSER)")
    parser.add_argument("--stop", action="store_true", help="stop a running launcher and its containers")
    parser.add_argument("--install-desktop", action="store_true",
                        help="add the launcher to the applications menu (~/.local/share/applications)")
    parser.add_argument("--self-check", action="store_true", help="check that the files it carries are all there")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser.parse_args(argv)


def run_window(launcher: Launcher, browser: Optional[str], reconfigure: bool, opener: Optional[str] = None) -> int:
    """Open the page in a Chrome window and wait for it to close. Without Chrome the page opens in the default
    browser with `opener` (xdg-open), which gives no sign of the close: the launcher then waits for `--stop`."""
    server = BootstrapServer(launcher)
    server.start()
    stop_event = threading.Event()
    previous = {}
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[number] = signal.signal(number, lambda *_: stop_event.set())
    try:
        launcher.begin(reconfigure)
        command = (browser_command(browser, server.url, launcher.layout.profile_dir) if browser
                   else [opener or "xdg-open", server.url])
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            message = f"The browser could not be started: {error}"
            log.error(message)
            notify(message)
            return 1
        log.info("window opened at %s", server.url)
        if browser:
            wait_for_window(process, stop_event)
            log.info("window closed")
        else:
            notify("Opened in your default browser. To stop Vacation Memories use its menu action "
                   "\"Stop Vacation Memories\" or run the launcher with --stop.")
            stop_event.wait()
            log.info("stop requested")
    finally:
        launcher.shutdown()
        server.stop()
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 0


def stop_running(layout: Layout, dist: DistConfig, source: Path, *, kill=os.kill, sleep=time.sleep,
                 clock=time.monotonic, timeout: float = 90.0, runner: Optional[Runner] = None) -> int:
    """`--stop`: ask the running launcher to close; when none is running, stop what an earlier run left up."""
    with single_instance(layout.lock_file) as idle:
        if idle:
            saved = load_answers(layout)
            if saved is None or not layout.env_file.is_file() or not layout.data_dir.is_dir():
                print("Nothing to stop.")
                return 0
            launcher = Launcher(layout, dist, source, runner=runner)
            launcher.answers = validate_answers(saved, layout)[0]
            launcher.stack_started = True
            launcher.shutdown()
            print("Stopped.")
            return 0
    try:
        pid = int(layout.lock_file.read_text(encoding="utf-8").strip())
        kill(pid, signal.SIGTERM)
    except (OSError, ValueError) as error:
        print(f"The running launcher could not be reached: {error}", file=sys.stderr)
        return 1
    deadline = clock() + timeout
    while clock() < deadline:
        with single_instance(layout.lock_file) as idle:
            if idle:
                print("Stopped.")
                return 0
        sleep(0.5)
    print("The launcher did not stop in time.", file=sys.stderr)
    return 1


def main(argv: Optional[List[str]] = None, environ=os.environ) -> int:
    args = parse_args(argv)
    root = payload_root()
    dist = load_dist_config(root)
    if args.version:
        print(dist.version)
        return 0
    if args.self_check:
        return self_check(root)
    layout = default_layout(dist.version, environ)
    if args.install_desktop:
        data_home = Path(environ.get("XDG_DATA_HOME") or Path(environ.get("HOME") or Path.home()) / ".local" / "share")
        print(install_desktop(data_home / "applications", own_command()))
        return 0

    configure_logging(layout)
    if args.stop:
        return stop_running(layout, dist, root)
    requested = args.browser or environ.get("LAUNCHER_BROWSER")
    browser = find_browser(requested)
    opener = None
    if not browser:
        opener = shutil.which("xdg-open") if not requested else None
        if not opener:
            message = (f"The browser {requested} was not found." if requested else
                       "Google Chrome was not found and xdg-open is not available. Install Google Chrome.")
            log.error(message)
            notify(message)
            print(message, file=sys.stderr)
            return 1
        log.warning("Google Chrome was not found: using the default browser with %s", opener)
    with single_instance(layout.lock_file) as acquired:
        if not acquired:
            log.info("already running")
            return 0
        return run_window(Launcher(layout, dist, root), browser, args.reconfigure, opener)


if __name__ == "__main__":
    sys.exit(main())
