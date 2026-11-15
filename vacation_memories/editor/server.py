"""The editor's HTTP server: the JSON API of section 9 of `UI-SPEC.md` and the static files of the interface.

Standard library only (`http.server.ThreadingHTTPServer`, D8): progress is polled and single-range `Range`
requests are implemented by hand, which is what the browser's `<video>` element needs to seek.

Only requests from the editor's own page are served: `Host` must be one of the allowed ones (DNS rebinding),
every request that changes something carries a matching `Origin` and `Content-Type: application/json`
(cross-site forms), and there are no CORS headers. Nothing in a URL or a response is an absolute host path;
media, clips and tracks are named by opaque ids.
"""

import json
import logging
import mimetypes
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from vacation_memories import __version__
from vacation_memories.core.config import Settings
from vacation_memories.core.process import ShutdownController, ToolRunner
from vacation_memories.core.runtime_profile import DetectionEnv, detect_ollama
from vacation_memories.editor import jobs as job_module
from vacation_memories.editor import media
from vacation_memories.editor.catalog import build_catalog, file_id, music_tracks, transition_library
from vacation_memories.editor.errors import ApiError, bad_request, conflict, not_found
from vacation_memories.editor.ids import is_opaque_id, opaque_id
from vacation_memories.editor.jobs import JobManager
from vacation_memories.editor.projects import DEFAULT_NAME, Project, Projects, revision_of, valid_video_name
from vacation_memories.planning.edit_plan import (
    DIP,
    DISSOLVE,
    FADE_IN,
    FADE_OUT,
    EdgeFades,
    EditPlan,
    PlanError,
    back_up_plan,
    finalize_timeline,
    plan_from_dict,
    save_plan,
)
from vacation_memories.planning.plan_rules import check_plan, plan_duration, plan_limits
from vacation_memories.rendering.music import MusicError, music_track_duration
from vacation_memories.rendering.render_config import RenderConfig

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:8080", "localhost:8080")
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_DISCARD_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
DURATION_TOLERANCE = 0.05  # of the requested duration (section 14)
STATIC_DIR = Path(__file__).parent / "static"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                               "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
}
_PLAN_FIELD = re.compile(r"'?((?:elements|music|excluded|output)[\w\[\].]*)'?(?:: '(\w+)')?")


class FileResponse:
    """A file to stream, with `Range` support for the videos."""

    def __init__(self, path: Path, content_type: str, *, download: str | None = None, cache: str = "no-cache"):
        self.path = path
        self.content_type = content_type
        self.download = download
        self.cache = cache


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """The `(first, last)` bytes of a single-range `Range` header; None for no (usable) range, "invalid" if it
    cannot be satisfied. Several ranges are not supported and are treated as no range at all (RFC 9110)."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    first, _, last = header[len("bytes="):].strip().partition("-")
    try:
        if first == "":  # the last N bytes
            length = int(last)
            if length <= 0:
                return "invalid"
            return max(0, size - length), size - 1
        start = int(first)
        end = size - 1 if last == "" else min(int(last), size - 1)
    except ValueError:
        return None
    if start >= size or end < start:
        return "invalid"
    return start, end


def plan_error_field(message: str) -> str | None:
    found = _PLAN_FIELD.match(message)
    if not found:
        return None
    return f"{found.group(1)}.{found.group(2)}" if found.group(2) else found.group(1)


class EditorApp:
    """The state and the endpoints of the editor; the handler only turns HTTP into calls to it."""

    def __init__(
        self,
        settings: Settings,
        *,
        allowed_hosts: Sequence[str] = DEFAULT_ALLOWED_HOSTS,
        static_dir: Path = STATIC_DIR,
        job_command: Sequence[str] | None = None,
        job_env: Mapping[str, str] | None = None,
        ai_check: Callable[[], dict] | None = None,
        controller: ShutdownController | None = None,
    ):
        self.settings = settings
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.static_dir = Path(static_dir)
        self.projects = Projects(settings)
        self.jobs = JobManager(job_command, job_env)
        self.controller = controller or ShutdownController()
        self.runner = ToolRunner(self.controller)
        self.rules = RenderConfig.from_settings(settings).rules
        self._ai_check = ai_check or self._check_ollama
        self._documents: dict[str, tuple[tuple, dict]] = {}
        self._track_lengths: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        """Stop what the server started: the running job (orderly, within the grace period)."""
        self.jobs.stop_all()

    # --- checks of every request ---

    def check_host(self, host: str | None) -> None:
        if not host or host.lower() not in self.allowed_hosts:
            raise ApiError(403, "forbidden_host", "this address is not allowed")

    def check_origin(self, origin: str | None) -> None:
        if not origin or origin.lower() not in {f"http://{host}" for host in self.allowed_hosts}:
            raise ApiError(403, "forbidden_origin", "the request does not come from the editor's page")

    # --- small endpoints ---

    def health(self) -> dict:
        return {"status": "ok", "version": __version__}

    def ai(self) -> dict:
        return self._ai_check()

    def _check_ollama(self) -> dict:
        settings = self.settings
        model = settings.model
        info = detect_ollama(DetectionEnv(), settings.ollama_url, model, lambda message: logger.info(message))
        reason = None
        if not info.reachable:
            reason = "ollama_unreachable"
        elif not info.model_present:
            reason = "model_missing"
        return {"available": reason is None, "reachable": info.reachable, "model": model,
                "model_present": info.model_present, "reason": reason}

    def folders(self) -> dict:
        return {"folders": self.projects.folders()}

    def open_project(self, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict) or "folder" not in body:
            raise bad_request("invalid_folder", "the body must be {\"folder\": <relative path>}", field="folder")
        project = self.projects.open(body["folder"])
        return (201 if project.created else 200), {"id": project.id, "folder": project.folder,
                                                    "state": project.state()}

    # --- project ---

    def project_state(self, project: Project) -> dict:
        videos = project.videos()
        plan, job = project.plan_info(), self.jobs.current(project.id, project.workdir)
        preview = videos["preview"]
        # A plan saved while the preview was rendering is newer than the version the render read, but older than the
        # video's own date: the revision the job started from tells.
        if (preview["present"] and not preview["outdated"] and job and job["type"] == job_module.PREVIEW
                and job["state"] == "done" and job["name"] is not None and preview["name"] == f"{job['name']}_preview"
                and job["plan_revision"] and plan["revision"] and job["plan_revision"] != plan["revision"]):
            preview = {**preview, "outdated": True}
        return {
            "id": project.id, "folder": project.folder, "settings": project.settings(),
            "analysis": project.analysis(), "plan": plan, "preview": preview,
            "exports": videos["exports"], "job": job, "busy": self._busy_elsewhere(project),
        }

    def _busy_elsewhere(self, project: Project) -> bool:
        return self.jobs.running_project() != project.id and project.busy_elsewhere()

    def save_settings(self, project: Project, body: object) -> dict:
        return project.save_settings(body)

    # --- plan ---

    def _document(self, project: Project) -> dict | None:
        """`candidates.json`, parsed again only when it changed."""
        try:
            info = project.candidates_path.stat()
        except OSError:
            return None
        signature = (info.st_mtime_ns, info.st_size)
        with self._lock:
            cached = self._documents.get(project.id)
            if cached and cached[0] == signature:
                return cached[1]
        try:
            document = json.loads(project.candidates_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        with self._lock:
            self._documents[project.id] = (signature, document)
        return document

    def _media_durations(self, project: Project) -> dict[str, float]:
        document = self._document(project) or {}
        return {entry["path"]: entry["duration"] for entry in document.get("files", [])
                if entry.get("type") == "video" and entry.get("duration")}

    def _track_durations(self, plan: EditPlan) -> dict[str, float]:
        """The length of the tracks the plan uses, for the bounds of their parts."""
        root = self.projects.music_root
        lengths: dict[str, float] = {}
        if plan.music is None or root is None:
            return lengths
        for track in plan.music.tracks:
            path = root / track.file
            try:
                info = path.stat()
            except OSError:
                continue
            key = (track.file, info.st_size, info.st_mtime_ns)
            with self._lock:
                known = self._track_lengths.get(key)
            if known is None:
                try:
                    known = music_track_duration(path, self.runner, track.file)
                except MusicError:
                    continue  # unreadable: the bounds of its parts are not checked, the renderer will say
                with self._lock:
                    self._track_lengths[key] = known
            lengths[track.file] = known
        return lengths

    def _read_plan(self, project: Project) -> tuple[EditPlan, str]:
        try:
            raw = project.plan_path.read_bytes()
        except FileNotFoundError:
            raise ApiError(404, "no_plan", "this project has no plan yet") from None
        except OSError as error:
            raise ApiError(500, "plan_unreadable", f"cannot read the plan: {error.strerror}") from None
        try:
            plan = plan_from_dict(json.loads(raw.decode("utf-8")), fades=self._fades())
        except (ValueError, PlanError) as error:
            raise ApiError(422, "invalid_plan", f"the plan on disk is not valid: {error}",
                           field=plan_error_field(str(error))) from None
        return plan, revision_of(raw)

    def _fades(self) -> EdgeFades:
        return EdgeFades(self.settings.fade_in_seconds, self.settings.fade_out_seconds)

    def _plan_document(self, project: Project, plan: EditPlan, revision: str) -> dict:
        durations = self._media_durations(project)
        limits = plan_limits(plan, self.rules, media_durations=durations)
        duration = plan_duration(plan, self.rules)
        # What names an element in a URL: the preview of its clip and the thumbnail of its file (opaque ids, section 9).
        refs = {element.id: {"clip": opaque_id(element.id), "media": file_id(element.source)} for element in plan.elements}
        return {"revision": revision, "plan": plan.to_dict(), "limits": limits.to_dict(), "duration": duration,
                "warnings": self._warnings(project, plan, duration), "refs": refs}

    def _warnings(self, project: Project, plan: EditPlan, duration: float) -> list[dict]:
        warnings = []
        for element in plan.elements:
            if not (project.input_dir / element.source).is_file():
                warnings.append({"code": "media_missing", "element": element.id,
                                 "message": "the file of this clip is no longer in the folder; it is left out of the render"})
        requested = plan.requested_duration
        if requested and abs(duration - requested) > DURATION_TOLERANCE * requested:
            warnings.append({"code": "duration_off", "message":
                             f"the video lasts {duration:.1f} s instead of the requested {requested:.1f} s"})
        for message in (plan.runtime or {}).get("warnings", []):
            if isinstance(message, str):
                warnings.append({"code": "planner", "message": message})
        return warnings

    def get_plan(self, project: Project) -> dict:
        plan, revision = self._read_plan(project)
        return self._plan_document(project, plan, revision)

    def put_plan(self, project: Project, body: object, if_match: str | None) -> dict:
        if if_match is None:
            raise ApiError(428, "if_match_required", "send the revision you read in If-Match")
        if self.jobs.running_type(project.id) == job_module.PLAN:
            raise conflict("plan_locked", "the plan cannot be saved while the analysis and plan job runs")
        if self._busy_elsewhere(project):
            raise conflict("project_busy", "another process is using this project: it is read-only until it ends")
        with project._lock:
            overwrite = if_match.strip().strip('"') == "*"
            try:
                _, current = self._read_plan(project)
            except ApiError as error:
                if not (overwrite and error.code == "invalid_plan"):  # a broken plan may be replaced on purpose
                    raise
                current = None
            if not overwrite and if_match.strip().strip('"') != current:
                raise conflict("revision_conflict", "the plan changed elsewhere", revision=current)
            try:
                plan = plan_from_dict(body, fades=self._fades())
            except PlanError as error:
                raise bad_request("invalid_plan", str(error), field=plan_error_field(str(error))) from None
            violations = check_plan(plan, self.rules, media_durations=self._media_durations(project),
                                    track_durations=self._track_durations(plan))
            if violations:
                first = violations[0]
                raise bad_request("invalid_plan", str(first), field=first.path,
                                  violations=[{"field": item.path, "message": item.message} for item in violations])
            plan, _layout = finalize_timeline(plan, self.rules)
            if overwrite:
                back_up_plan(project.plan_path)
            save_plan(plan, project.plan_path)
            revision = revision_of(project.plan_path.read_bytes())
        return self._plan_document(project, plan, revision)

    # --- catalog and media ---

    def catalog(self, project: Project) -> dict:
        document = self._document(project)
        try:
            plan, _ = self._read_plan(project)
        except ApiError:
            plan = None
        settings = self.settings
        defaults = {FADE_IN: settings.fade_in_seconds, FADE_OUT: settings.fade_out_seconds,
                    DISSOLVE: self.rules.default_seconds, DIP: self.rules.default_seconds}
        library = transition_library(defaults, self.rules.min_seconds)
        catalog = build_catalog(document, plan, music_tracks(self.projects.music_root), library)
        catalog["revision"] = None if plan is None else revision_of(project.plan_path.read_bytes())
        return catalog

    def thumb(self, project: Project, media_id: str, at: str | None) -> FileResponse:
        document = self._document(project)
        entry = next((item for item in (document or {}).get("files", []) if file_id(item["path"]) == media_id), None)
        if entry is None:
            raise not_found("media")
        seconds = None
        if at is not None:
            try:
                seconds = float(at)
            except ValueError:
                raise bad_request("invalid_time", "t must be a number of seconds", field="t") from None
            if not 0 <= seconds < 1e6:
                raise bad_request("invalid_time", "t must be a number of seconds", field="t")
        path = media.thumbnail(project.workdir, project.input_dir / entry["path"], entry["type"], self.runner, seconds)
        return FileResponse(path, "image/jpeg", cache="private, max-age=3600")

    def waveform(self, project: Project, track_id: str) -> FileResponse:
        track = next((item for item in music_tracks(self.projects.music_root) if item["id"] == track_id), None)
        if track is None:
            raise not_found("track")
        path = media.waveform(project.workdir, self.projects.music_root / track["file"], self.runner)
        return FileResponse(path, "application/json", cache="private, max-age=3600")

    def clip(self, project: Project, element_id: str) -> FileResponse:
        plan, _ = self._read_plan(project)
        element = next((item for item in plan.elements if opaque_id(item.id) == element_id), None)
        if element is None:
            raise not_found("element")
        path = media.clip_preview(project.workdir, project.input_dir, element, plan.output, self.runner)
        return FileResponse(path, "video/mp4")

    def video(self, project: Project, name: str, download: bool) -> FileResponse:
        path = project.video_path(name)
        return FileResponse(path, "video/mp4", download=f"{name}.mp4" if download else None)

    # --- jobs ---

    def start_job(self, project: Project, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict) or body.get("type") not in job_module.JOB_TYPES:
            raise bad_request("invalid_job", "type must be one of: " + ", ".join(job_module.JOB_TYPES), field="type")
        kind = body["type"]
        if self.jobs.running_project() is not None:
            raise conflict("job_running", "another job is already running")
        if self._busy_elsewhere(project):
            raise conflict("project_busy", "another process is using this project")
        settings = project.settings()
        if kind == job_module.PLAN:
            arguments = job_module.plan_arguments(project.input_dir, project.workdir, settings,
                                                  ai=self._wants_ai(settings, body))
            name = None
        else:
            if not project.plan_path.is_file():
                raise conflict("no_plan", "there is no plan to render yet")
            name = body.get("name", DEFAULT_NAME)
            if not isinstance(name, str) or not valid_video_name(name):
                raise bad_request("invalid_name", "the name must be a plain file name without extension", field="name")
            arguments = job_module.render_arguments(project.input_dir, project.workdir, settings, name=name,
                                                    preview=kind == job_module.PREVIEW)
        revision = None if kind == job_module.PLAN else project.plan_info()["revision"]
        job = self.jobs.start(project.id, project.workdir, kind, arguments, name=name, plan_revision=revision)
        return 202, job.to_dict()

    def _wants_ai(self, settings: dict, body: dict) -> bool:
        """With AI unless the project or this request says no; with AI Ollama must answer now (section 6)."""
        requested = body.get("ai", settings["ai"])
        if requested is not None and not isinstance(requested, bool):
            raise bad_request("invalid_job", "ai must be true, false or absent", field="ai")
        if requested is False:
            return False
        status = self.ai()
        if status["available"]:
            return True
        if requested is None:
            return False  # automatic: without AI when it is not available, and the job says so
        raise conflict("ai_unavailable", "Ollama or its model is not available: wait for it, or analyze without AI",
                       reason=status["reason"], model=status["model"])

    def status(self, project: Project) -> dict:
        job = self.jobs.current(project.id, project.workdir)
        progress = job_module.progress(project.workdir)
        if job and progress and (progress.get("started_at") or "") < job["started_at"]:
            progress = None  # `status.json` is still the previous run's: the new child has not written its own
        return {"job": job, "progress": progress, "busy": self._busy_elsewhere(project)}

    def cancel(self, project: Project) -> dict:
        return {"stopping": self.jobs.cancel(project.id)}


def make_server(app: EditorApp, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(EditorHandler):
        pass

    Handler.app = app
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


_ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method: str, pattern: str):
    def register(function):
        _ROUTES.append((method, re.compile(pattern + r"\Z"), function.__name__))
        return function

    return register


class EditorHandler(BaseHTTPRequestHandler):
    app: EditorApp
    protocol_version = "HTTP/1.1"
    server_version = "VacationMemories"
    sys_version = ""

    # --- plumbing ---

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("%s %s", self.address_string(), format % args)  # never the query string, which may hold a time

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:
        self._dispatch("GET", head=True)

    def do_OPTIONS(self) -> None:
        self._send_error(ApiError(405, "method_not_allowed", "there is no CORS: OPTIONS is not served"))

    def _dispatch(self, method: str, head: bool = False) -> None:
        try:
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            self.query = {key: values[0] for key, values in parse_qs(parts.query).items()}
            if path != "/api/health":
                self.app.check_host(self.headers.get("Host"))
            if method != "GET":
                self.app.check_origin(self.headers.get("Origin"))
                if not self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/json":
                    raise ApiError(415, "unsupported_media_type", "send Content-Type: application/json")
            if not path.startswith("/api/"):
                if method != "GET":
                    raise ApiError(405, "method_not_allowed", "method not allowed")
                self._send_file(self._static(path), head)
                return
            self._route(method, path[len("/api"):], head)
        except ApiError as error:
            self._send_error(error)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            logger.exception("unexpected error serving %s", self.command)
            self._send_error(ApiError(500, "internal_error", "internal error"))

    def _route(self, method: str, path: str, head: bool) -> None:
        known = False
        for verb, pattern, name in _ROUTES:
            found = pattern.match(path)
            if not found:
                continue
            known = True
            if verb != method:
                continue
            result = getattr(self, name)(**found.groupdict())
            if isinstance(result, FileResponse):
                self._send_file(result, head)
            elif isinstance(result, tuple):
                self._send_json(result[1], result[0])
            else:
                self._send_json(result)
            return
        raise ApiError(405, "method_not_allowed", "method not allowed") if known else not_found("endpoint")

    def _project(self, identifier: str) -> Project:
        return self.app.projects.get(identifier)

    def _body(self) -> object:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise bad_request("invalid_json", "invalid Content-Length") from None
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "body_too_large", "the body is too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise bad_request("invalid_json", "the body is not valid JSON") from None

    def _discard_body(self) -> None:
        """Read and drop what the client is still sending, so closing does not reset the connection under it."""
        try:
            remaining = min(int(self.headers.get("Content-Length", "0")), MAX_DISCARD_BYTES)
        except ValueError:
            return
        try:
            while remaining > 0 and (chunk := self.rfile.read(min(CHUNK_BYTES, remaining))):
                remaining -= len(chunk)
        except OSError:
            pass

    # --- responses ---

    def _headers(self, status: int, content_type: str, length: int, extra: Mapping[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send_json(self, body: object, status: int = 200) -> None:
        data = json.dumps(body, ensure_ascii=True).encode("ascii")  # file names that are not UTF-8 stay escaped
        self._headers(status, "application/json; charset=utf-8", len(data), {"Cache-Control": "no-store"})
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_error(self, error: ApiError) -> None:
        try:
            self._send_json(error.to_dict(), error.status)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        if error.status in (413, 415):
            self._discard_body()
            self.close_connection = True  # the unread body would be taken for the next request

    def _static(self, path: str) -> "FileResponse":
        relative = path.lstrip("/") or "index.html"
        root = self.app.static_dir.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            target = root / "index.html"  # the interface routes itself; unknown paths get the page
            if not target.is_file():
                raise not_found("page")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        return FileResponse(target, content_type)

    def _send_file(self, response: FileResponse, head: bool = False) -> None:
        try:
            handle = open(response.path, "rb")
        except OSError:
            raise not_found("file") from None
        with handle:
            size = response.path.stat().st_size
            selection = parse_range(self.headers.get("Range"), size)
            extra = {"Accept-Ranges": "bytes", "Cache-Control": response.cache}
            if response.download:
                extra["Content-Disposition"] = f'attachment; filename="{response.download}"'
            if selection == "invalid":
                self._headers(416, response.content_type, 0, {**extra, "Content-Range": f"bytes */{size}"})
                return
            if selection is None:
                status, first, last = 200, 0, size - 1
            else:
                status, (first, last) = 206, selection
                extra["Content-Range"] = f"bytes {first}-{last}/{size}"
            length = max(0, last - first + 1)
            self._headers(status, response.content_type, length, extra)
            if head:
                return
            handle.seek(first)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # --- endpoints ---

    @route("GET", r"/health")
    def health(self):
        return self.app.health()

    @route("GET", r"/ai")
    def ai(self):
        return self.app.ai()

    @route("GET", r"/folders")
    def folders(self):
        return self.app.folders()

    @route("POST", r"/projects")
    def open_project(self):
        return self.app.open_project(self._body())

    @route("GET", r"/projects/(?P<p>[^/]+)")
    def project(self, p):
        return self.app.project_state(self._project(p))

    @route("PUT", r"/projects/(?P<p>[^/]+)/settings")
    def settings(self, p):
        return self.app.save_settings(self._project(p), self._body())

    @route("GET", r"/projects/(?P<p>[^/]+)/plan")
    def get_plan(self, p):
        return self.app.get_plan(self._project(p))

    @route("PUT", r"/projects/(?P<p>[^/]+)/plan")
    def put_plan(self, p):
        return self.app.put_plan(self._project(p), self._body(), self.headers.get("If-Match"))

    @route("GET", r"/projects/(?P<p>[^/]+)/catalog")
    def catalog(self, p):
        return self.app.catalog(self._project(p))

    @route("GET", r"/projects/(?P<p>[^/]+)/thumb/(?P<media>[^/]+)")
    def thumb(self, p, media):
        if not is_opaque_id(media):
            raise not_found("media")
        return self.app.thumb(self._project(p), media, self.query.get("t"))

    @route("GET", r"/projects/(?P<p>[^/]+)/waveform/(?P<track>[^/]+)")
    def waveform(self, p, track):
        if not is_opaque_id(track):
            raise not_found("track")
        return self.app.waveform(self._project(p), track)

    @route("GET", r"/projects/(?P<p>[^/]+)/clip/(?P<element>[^/]+)")
    def clip(self, p, element):
        if not is_opaque_id(element):
            raise not_found("element")
        return self.app.clip(self._project(p), element)

    @route("POST", r"/projects/(?P<p>[^/]+)/jobs")
    def start_job(self, p):
        return self.app.start_job(self._project(p), self._body())

    @route("GET", r"/projects/(?P<p>[^/]+)/status")
    def status(self, p):
        return self.app.status(self._project(p))

    @route("POST", r"/projects/(?P<p>[^/]+)/jobs/cancel")
    def cancel(self, p):
        return self.app.cancel(self._project(p))

    @route("GET", r"/projects/(?P<p>[^/]+)/video/(?P<name>[^/]+)")
    def video(self, p, name):
        return self.app.video(self._project(p), name, self.query.get("download") == "1")


def serve(app: EditorApp, host: str, port: int, controller: ShutdownController) -> int:
    """Serve until a signal arrives, then stop the job in an orderly way; the exit code is 128 + the signal."""
    server = make_server(app, host, port)
    controller.on_shutdown(lambda: threading.Thread(target=server.shutdown, name="shutdown", daemon=True).start())
    logger.info("the editor listens on %s:%d (allowed hosts: %s)", host, port, ", ".join(sorted(app.allowed_hosts)))
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        app.close()
    return 128 + controller.signum if controller.signum else 0
