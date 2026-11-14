"""Projects: one input folder under the media root with its own workdir (sections 5 and 7 of `UI-SPEC.md`).

The workdir is `<work root>/projects/<slug>-<hash>/`: the slug comes from the folder name and the hash
from its relative path, so two folders with the same name do not collide (D9). The CLI works on the
same workdir with `--input <media>/<folder> --workdir <workdir>`.
"""

import fcntl
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vacation_memories.analysis.media_scanner import HEIC_EXTENSIONS, PHOTO_EXTENSIONS, VIDEO_EXTENSIONS, heic_available, scan_media
from vacation_memories.core.atomic import atomic_write_json
from vacation_memories.core.config import ConfigError, Settings, resolve_settings
from vacation_memories.core.workdir import LOCK_NAME
from vacation_memories.editor.errors import ApiError, bad_request, not_found
from vacation_memories.planning.edit_plan import PlanError, has_user_edits, load_plan

PROJECT_FILE = "project.json"
PROJECT_VERSION = 1
PROJECTS_DIR = "projects"
CANDIDATES_FILE = "candidates.json"
PLAN_FILE = "edit_plan.json"
DEFAULT_NAME = "memories"
SLUG_LENGTH = 40
HASH_LENGTH = 8
MAX_FOLDERS = 1000
ROOT_FOLDER = "."
_PROJECT_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{%d}\Z" % HASH_LENGTH)
_VIDEO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}\Z")
PREVIEW_SUFFIX = "_preview"
PARTIAL_SUFFIX = ".partial"

# What a new project starts with: the CLI's own defaults, so what the interface shows is what runs.
DEFAULT_SETTINGS: dict = {
    "ai": None,  # None: with the AI if Ollama and the model are available, otherwise without
    "ai_time_budget": 600,
    "ai_frames": None,
    "assume_tz": None,
    "duration": "5:00",
    "structure": "narrative",
    "aspect": "16:9",
    "resolution": "1080p",
    "music": [],
    "jobs": "auto",
    "hwaccel": "none",
    "nice": 0,
}
# What each change requires (section 7): the analysis again, a new plan, only the rendering, only the music block.
_REQUIRES = {
    "ai": "analysis", "ai_time_budget": "analysis", "ai_frames": "analysis", "assume_tz": "analysis",
    "duration": "plan", "structure": "plan",
    "aspect": "render", "resolution": "render",
    "music": "music",
}
_REQUIRES_ORDER = ("analysis", "plan", "render", "music")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().encode("ascii", "ignore").decode()).strip("-")
    return slug[:SLUG_LENGTH].strip("-") or "media"


def project_id(folder: str) -> str:
    """The id of the project of `folder` (relative to the media root, as POSIX)."""
    digest = hashlib.sha256(folder.encode("utf-8", errors="surrogateescape")).hexdigest()[:HASH_LENGTH]
    name = PurePosixPath(folder).name if folder != ROOT_FOLDER else "media"
    return f"{slugify(name)}-{digest}"


def valid_video_name(name: str) -> bool:
    """A name for the exported video (without extension): a plain file name, not one of the reserved ones."""
    return bool(_VIDEO_NAME.match(name)) and not name.endswith((PREVIEW_SUFFIX, PARTIAL_SUFFIX)) and ".." not in name


def revision_of(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class Project:
    id: str
    folder: str
    input_dir: Path
    workdir: Path
    _validator: "Projects"
    _lock: threading.Lock
    created: bool = False

    @property
    def plan_path(self) -> Path:
        return self.workdir / PLAN_FILE

    @property
    def candidates_path(self) -> Path:
        return self.workdir / CANDIDATES_FILE

    # --- settings ---

    def settings(self) -> dict:
        stored = self._read().get("settings", {})
        return {**DEFAULT_SETTINGS, **{key: value for key, value in stored.items() if key in DEFAULT_SETTINGS}}

    def save_settings(self, changes: object) -> dict:
        """Validate and save a partial set of settings; returns them with what the change requires."""
        if not isinstance(changes, dict):
            raise bad_request("invalid_settings", "the settings must be a JSON object")
        unknown = next((key for key in changes if key not in DEFAULT_SETTINGS), None)
        if unknown is not None:
            raise bad_request("invalid_settings", f"unknown setting: {unknown}", field=unknown)
        with self._lock:
            before = self.settings()
            after = {**before, **changes}
            if "ai_frames" in changes and changes["ai_frames"] is not None and "ai_time_budget" not in changes:
                after["ai_time_budget"] = None  # an exact number of frames replaces the time budget
            if "ai_time_budget" in changes and changes["ai_time_budget"] is not None and "ai_frames" not in changes:
                after["ai_frames"] = None
            self._validator.validate_settings(self, after)
            data = self._read()
            data["settings"] = after
            atomic_write_json(self.workdir / PROJECT_FILE, data)
        needed = {_REQUIRES[key] for key in after if key in _REQUIRES and after[key] != before[key]}
        return {"settings": after, "requires": [kind for kind in _REQUIRES_ORDER if kind in needed]}

    def _read(self) -> dict:
        try:
            data = json.loads((self.workdir / PROJECT_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": PROJECT_VERSION, "input": self.folder}
        return data if isinstance(data, dict) else {"version": PROJECT_VERSION, "input": self.folder}

    # --- state ---

    def busy_elsewhere(self) -> bool:
        """Whether a process holds the workdir lock (a job of the CLI). The caller knows its own jobs."""
        try:
            descriptor = os.open(self.workdir / LOCK_NAME, os.O_RDONLY)
        except OSError:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        finally:
            os.close(descriptor)
        return False

    def analysis(self) -> dict:
        """Files analyzed out of the files in the folder (section 5, "Analysis present")."""
        try:
            document = json.loads(self.candidates_path.read_text(encoding="utf-8"))
            analyzed = {entry["path"]: (entry["size"], entry["mtime_ns"]) for entry in document["files"]}
        except (OSError, ValueError, KeyError, TypeError):
            return {"present": False, "complete": False, "files": {"total": self._scan_total(), "analyzed": 0,
                                                                   "new_or_changed": None, "removed": 0},
                    "ai": None, "generated_at": None}
        found = {media.rel_path.as_posix(): (media.size, media.mtime_ns) for media in scan_media(self.input_dir)}
        changed = sum(1 for path, signature in found.items() if analyzed.get(path) != signature)
        removed = sum(1 for path in analyzed if path not in found)
        return {
            "present": True,
            "complete": changed == 0,
            "files": {"total": len(found), "analyzed": len(found) - changed, "new_or_changed": changed,
                      "removed": removed},
            "ai": document.get("ai"),
            "generated_at": document.get("generated_at"),
        }

    def _scan_total(self) -> int:
        return len(scan_media(self.input_dir)) if self.input_dir.is_dir() else 0

    def plan_info(self) -> dict:
        try:
            raw = self.plan_path.read_bytes()
        except FileNotFoundError:
            return {"present": False, "edited": False, "valid": True, "revision": None, "error": None}
        except OSError as error:
            return {"present": True, "edited": False, "valid": False, "revision": None, "error": str(error.strerror)}
        info = {"present": True, "revision": revision_of(raw), "valid": True, "error": None, "edited": False}
        try:
            info["edited"] = has_user_edits(load_plan(self.plan_path))
        except PlanError as error:
            info.update(valid=False, error=str(error))
        return info

    def videos(self) -> dict:
        """The preview and the exported videos in the workdir (the partial ones are offered for inspection)."""
        previews, exports = [], []
        for path in sorted(self.workdir.glob("*.mp4")):
            if not path.is_file():
                continue
            info = path.stat()
            stem = path.name[: -len(".mp4")]
            entry = {"name": stem, "size": info.st_size,
                     "modified": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                     "partial": stem.endswith(PARTIAL_SUFFIX)}
            (previews if stem.endswith(PREVIEW_SUFFIX) else exports).append((info.st_mtime, entry))
        plan_time = self.plan_path.stat().st_mtime if self.plan_path.exists() else None
        newest = max(previews, key=lambda item: item[0], default=None)
        preview = {"present": newest is not None, "name": None, "outdated": False}
        if newest is not None:
            preview.update(name=newest[1]["name"], outdated=plan_time is not None and plan_time > newest[0])
        return {"preview": preview, "exports": [entry for _, entry in exports]}

    def state(self) -> str:
        """not_analyzed, incomplete, analyzed, plan_edited or rendered (section 2, step 2)."""
        videos = self.videos()
        if any(not entry["partial"] for entry in videos["exports"]):
            return "rendered"
        analysis = self.analysis()
        if not analysis["present"]:
            return "not_analyzed"
        if not analysis["complete"]:
            return "incomplete"
        return "plan_edited" if self.plan_info()["edited"] else "analyzed"

    def video_path(self, name: str) -> Path:
        if not _VIDEO_NAME.match(name) or ".." in name:
            raise not_found("video")
        path = self.workdir / f"{name}.mp4"
        if not path.is_file():
            raise not_found("video")
        return path


class Projects:
    """The projects of an editor: the folders under the media root and their workdirs."""

    def __init__(self, settings: Settings):
        if settings.input is None:
            raise ConfigError("the editor needs the media root (--input or `input` in the configuration)")
        self.settings = settings
        self.media_root = Path(settings.input).absolute()
        self.music_root = None if settings.music_dir is None else Path(settings.music_dir).absolute()
        self.root = Path(settings.workdir).absolute() / PROJECTS_DIR
        self._lock = threading.Lock()
        self._project_locks: dict[str, threading.Lock] = {}

    # --- folders ---

    def folders(self) -> list[dict]:
        """The root and every folder under it with media, with the media of their subfolders counted in."""
        photo_extensions = PHOTO_EXTENSIONS | HEIC_EXTENSIONS if heic_available() else PHOTO_EXTENSIONS
        counts: dict[str, list[int]] = {}
        for folder, _dirs, names in os.walk(self.media_root):
            relative = os.path.relpath(folder, self.media_root)
            photos = sum(1 for name in names if os.path.splitext(name)[1].lower() in photo_extensions)
            videos = sum(1 for name in names if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS)
            if not photos and not videos:
                continue
            parts = [] if relative == "." else relative.split(os.sep)
            for depth in range(len(parts) + 1):
                key = ROOT_FOLDER if depth == 0 else "/".join(parts[:depth])
                entry = counts.setdefault(key, [0, 0])
                entry[0] += photos
                entry[1] += videos
        result = []
        for path in sorted(counts, key=lambda key: ("" if key == ROOT_FOLDER else key)):
            photos, videos = counts[path]
            identifier = project_id(path)
            exists = (self.root / identifier).is_dir()
            state = self._project(identifier, path).state() if exists else "not_analyzed"
            result.append({"path": path, "photos": photos, "videos": videos, "project": exists, "id": identifier,
                           "state": state})
            if len(result) >= MAX_FOLDERS:
                break
        return result

    def open(self, folder: object) -> Project:
        """The project of a folder, created if it is new."""
        relative = self._relative_folder(folder)
        identifier = project_id(relative)
        project = self._project(identifier, relative)
        project.workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (project.workdir / PROJECT_FILE).exists():
            project.created = True
            atomic_write_json(project.workdir / PROJECT_FILE, {
                "version": PROJECT_VERSION, "input": relative, "settings": dict(DEFAULT_SETTINGS),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
        return project

    def get(self, identifier: str) -> Project:
        if not _PROJECT_ID.match(identifier or ""):
            raise not_found("project")
        workdir = self.root / identifier
        try:
            stored = json.loads((workdir / PROJECT_FILE).read_text(encoding="utf-8"))
            relative = self._relative_folder(stored["input"])
        except (OSError, ValueError, KeyError, ApiError):
            raise not_found("project") from None
        if project_id(relative) != identifier:
            raise not_found("project")
        return self._project(identifier, relative)

    def _project(self, identifier: str, relative: str) -> Project:
        with self._lock:
            lock = self._project_locks.setdefault(identifier, threading.Lock())
        folder = self.media_root if relative == ROOT_FOLDER else self.media_root / relative
        return Project(identifier, relative, folder, self.root / identifier, self, lock)

    def _relative_folder(self, folder: object) -> str:
        if not isinstance(folder, str) or not folder or "\x00" in folder:
            raise bad_request("invalid_folder", "the folder must be a path relative to the media root", field="folder")
        path = PurePosixPath(folder)
        if path.is_absolute() or ".." in path.parts:
            raise bad_request("invalid_folder", "the folder must stay inside the media root", field="folder")
        relative = path.as_posix()
        resolved = (self.media_root / relative).resolve()
        if not resolved.is_relative_to(self.media_root.resolve()):
            raise bad_request("invalid_folder", "the folder must stay inside the media root", field="folder")
        if not resolved.is_dir():
            raise not_found("folder")
        return relative

    # --- validation of the settings ---

    def validate_settings(self, project: Project, settings: dict) -> None:
        """Run the settings through the CLI's own validation, so the messages are the same (section 7)."""
        for key, expected in (("ai", (bool, type(None))), ("music", (list,))):
            if not isinstance(settings[key], expected):
                raise bad_request("invalid_settings", f"{key} has the wrong type", field=key)
        music = self._check_music(settings["music"])
        options = {"input": project.input_dir, "workdir": project.workdir, "music": music, "music_dir": self.music_root,
                   **{key: settings[key] for key in ("duration", "structure", "aspect", "resolution", "assume_tz",
                                                     "ai_time_budget", "ai_frames", "jobs", "hwaccel", "nice")}}
        options["no_ai"] = settings["ai"] is False
        try:
            resolve_settings({key: value for key, value in options.items() if value is not None}, _VALIDATION_ENV)
        except ConfigError as error:
            raise bad_request("invalid_settings", str(error), field=_field_of(str(error), settings)) from None

    def _check_music(self, tracks: list) -> Path | None:
        for track in tracks:
            if not isinstance(track, str) or not track or PurePosixPath(track).is_absolute() \
                    or ".." in PurePosixPath(track).parts:
                raise bad_request("invalid_settings", f"invalid music track: {track!r}", field="music")
            if self.music_root is None:
                raise bad_request("invalid_settings", "there is no music folder", field="music")
            if not (self.music_root / track).is_file():
                raise bad_request("invalid_settings", f"music track not found: {track}", field="music")
        return Path(tracks[0]) if tracks else None


# The environment must not change the validation: only the CLI options and the defaults count here.
_VALIDATION_ENV: dict[str, str] = {}
_FIELD_IN_MESSAGE = re.compile(r"invalid value for (\w+) in")
_OPTION_FIELDS = {"--duration": "duration", "--structure": "structure", "--aspect": "aspect",
                  "--resolution": "resolution", "--ai-frames": "ai_frames", "--ai-time-budget": "ai_time_budget"}


def _field_of(message: str, settings: dict) -> str | None:
    found = _FIELD_IN_MESSAGE.search(message)
    if found and found.group(1) in DEFAULT_SETTINGS:
        return found.group(1)
    if "--ai-frames" in message and "--ai-time-budget" in message:
        return None  # a contradiction between two settings, not one field
    for option, field in _OPTION_FIELDS.items():
        if option in message:
            return field
    return next((key for key in settings if key in message), None)
