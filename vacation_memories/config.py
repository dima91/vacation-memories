"""Single configuration for CLI, environment variables and TOML file.

Precedence: CLI, then `VACATION_MEMORIES_*` variables, then the configuration file, finally the
defaults. `OLLAMA_HOST`, a variable of another program, counts only as a fallback for the Ollama
URL after the configuration file. An empty variable counts as unset.
"""

import ipaddress
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from vacation_memories.ollama_client import DEFAULT_TIMEOUT_SECONDS
from vacation_memories.transitions import DEFAULT_DISSOLVE_SECONDS, MAX_DISSOLVE_FRACTION, MIN_DISSOLVE_SECONDS

ENV_PREFIX = "VACATION_MEMORIES_"
CONFIG_ENV = ENV_PREFIX + "CONFIG"
DEFAULT_MODEL = "qwen3-vl:4b-instruct-q4_K_M"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
OLLAMA_DEFAULT_PORT = 11434
DEFAULT_AI_TIME_BUDGET_SECONDS = 600.0  # used when neither --ai-frames nor --ai-time-budget is given
DEFAULT_AI_RESOLUTION = 512  # long side of the image sent to the model
MIN_AI_RESOLUTION = 128
MAX_AI_RESOLUTION = 768
DEFAULT_FADE_IN_SECONDS = 1.0  # from black at the start of the video; at most 1 s
DEFAULT_FADE_OUT_SECONDS = 3.0  # to black at the end, in sync with the fade-out of the music
DEFAULT_DURATION_TOLERANCE = 0.2  # seconds between the final file and the planned duration
MAX_NICE = 19
_SOURCE_RANK = {"cli": 0, "env": 1, "config": 2}


class ConfigError(Exception):
    """Invalid usage or configuration (exit code 2)."""


@dataclass(frozen=True)
class Settings:
    """Resolved configuration. `sources` tells where each value came from."""

    workdir: Path
    log_file: Path
    input: Path | None
    log_format: str
    quiet: bool
    nice: int
    assume_tz: str | None
    jobs: int | str
    hwaccel: str
    threads_per_job: int | None
    block_size: int | None
    cache_memory_mb: int | None
    ollama_url: str
    allow_remote_ollama: bool
    require_ai: bool
    no_ai: bool
    model: str
    ai_frames: int | None
    ai_time_budget: float | None
    ai_resolution: int
    ai_timeout: float
    ai_max_concurrency: int
    ai_unload_model: bool
    transition_duration: float
    transition_max_fraction: float
    transition_min_duration: float
    fade_in_seconds: float
    fade_out_seconds: float
    duration_tolerance: float
    sources: dict[str, str] = field(default_factory=dict)


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    raise ValueError("expected true or false")


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("expected an integer")
    try:
        return int(value)
    except ValueError:
        raise ValueError("expected an integer") from None


def _positive_integer(value: object) -> int:
    number = _integer(value)
    if number < 1:
        raise ValueError("must be at least 1")
    return number


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("expected a number")
    try:
        number = float(value)
    except ValueError:
        raise ValueError("expected a number") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("expected a finite number")
    return number


def _non_negative_float(value: object) -> float:
    number = _float(value)
    if number < 0:
        raise ValueError("must not be negative")
    return number


def _positive_float(value: object) -> float:
    number = _float(value)
    if number <= 0:
        raise ValueError("must be greater than 0")
    return number


def _ai_resolution(value: object) -> int:
    number = _integer(value)
    if not MIN_AI_RESOLUTION <= number <= MAX_AI_RESOLUTION:
        raise ValueError(f"must be between {MIN_AI_RESOLUTION} and {MAX_AI_RESOLUTION}")
    return number


def _fraction(value: object) -> float:
    number = _float(value)
    if not 0 < number <= 0.5:
        raise ValueError("must be greater than 0 and at most 0.5")
    return number


def _nice(value: object) -> int:
    number = _integer(value)
    if not 0 <= number <= MAX_NICE:
        raise ValueError(f"must be between 0 and {MAX_NICE}")
    return number


def _jobs(value: object) -> int | str:
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    return _positive_integer(value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected a non-empty string")
    return value.strip()


def _path(value: object) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError("expected a path")
    return Path(value)


def _choice(*allowed: str) -> Callable[[object], str]:
    def parse(value: object) -> str:
        if value not in allowed:
            raise ValueError(f"expected one of: {', '.join(allowed)}")
        return value  # type: ignore[return-value]

    return parse


def _time_zone(value: object) -> str:
    name = _text(value)
    try:
        ZoneInfo(name)
    except Exception:
        raise ValueError(f"unknown time zone: {name}") from None
    return name


def _hwaccel(value: object) -> str:
    name = _text(value)
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise ValueError("expected auto, none or an accelerator name such as vaapi")
    return name


def _ollama_url(value: object) -> str:
    url = _text(value)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the Ollama URL must start with http:// or https://")
    return url.rstrip("/")


_PARSERS: dict[str, Callable[[object], object]] = {
    "workdir": _path,
    "log_file": _path,
    "input": _path,
    "log_format": _choice("text", "json"),
    "quiet": _boolean,
    "nice": _nice,
    "assume_tz": _time_zone,
    "jobs": _jobs,
    "hwaccel": _hwaccel,
    "threads_per_job": _positive_integer,
    "block_size": _positive_integer,
    "cache_memory_mb": _positive_integer,
    "ollama_url": _ollama_url,
    "allow_remote_ollama": _boolean,
    "require_ai": _boolean,
    "no_ai": _boolean,
    "model": _text,
    "ai_frames": _positive_integer,
    "ai_time_budget": _positive_float,
    "ai_resolution": _ai_resolution,
    "ai_timeout": _positive_float,
    "ai_max_concurrency": _positive_integer,
    "ai_unload_model": _boolean,
    "transition_duration": _non_negative_float,
    "transition_max_fraction": _fraction,
    "transition_min_duration": _non_negative_float,
    "fade_in_seconds": _non_negative_float,
    "fade_out_seconds": _non_negative_float,
    "duration_tolerance": _non_negative_float,
}
_DEFAULTS: dict[str, object] = {
    "workdir": Path("work"),
    "log_file": None,
    "log_format": "text",
    "quiet": False,
    "nice": 0,
    "jobs": "auto",
    "hwaccel": "none",
    "ollama_url": DEFAULT_OLLAMA_URL,
    "allow_remote_ollama": False,
    "require_ai": False,
    "no_ai": False,
    "model": DEFAULT_MODEL,
    "ai_frames": None,
    "ai_time_budget": None,
    "ai_resolution": DEFAULT_AI_RESOLUTION,
    "ai_timeout": DEFAULT_TIMEOUT_SECONDS,
    "ai_max_concurrency": 1,
    "ai_unload_model": True,
    "input": None,
    "assume_tz": None,
    "threads_per_job": None,
    "block_size": None,
    "cache_memory_mb": None,
    "transition_duration": DEFAULT_DISSOLVE_SECONDS,
    "transition_max_fraction": MAX_DISSOLVE_FRACTION,
    "transition_min_duration": MIN_DISSOLVE_SECONDS,
    "fade_in_seconds": DEFAULT_FADE_IN_SECONDS,
    "fade_out_seconds": DEFAULT_FADE_OUT_SECONDS,
    "duration_tolerance": DEFAULT_DURATION_TOLERANCE,
}


def normalize_ollama_host(host: str) -> str:
    """Turn an `OLLAMA_HOST` value into a URL the way Ollama does.

    No scheme means `http://`, no port means 11434 (80 or 443 when a scheme is given), and
    `0.0.0.0` counts as `localhost`.
    """
    host = host.strip()
    scheme, separator, rest = host.partition("://")
    if not separator:
        scheme, rest, default_port = "http", host, OLLAMA_DEFAULT_PORT
    else:
        default_port = 443 if scheme == "https" else 80
    parts = urlsplit("//" + rest)
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"invalid port in OLLAMA_HOST: {host}") from None
    hostname = parts.hostname or "localhost"
    if hostname == "0.0.0.0":
        hostname = "localhost"
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"{scheme}://{hostname}:{port or default_port}{parts.path.rstrip('/')}"


def ollama_url_is_local(url: str) -> bool:
    hostname = urlsplit(url).hostname or ""
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def resolve_settings(cli: Mapping[str, object], env: Mapping[str, str]) -> Settings:
    """Merge the layers; raise `ConfigError` on any invalid value or contradiction.

    `cli` holds only the options the user actually passed, under their setting names, plus
    `config` for the configuration file path.
    """
    cli = {key: value for key, value in cli.items() if value is not None}
    file_values, file_name = _load_config_file(cli.get("config"), env)
    layers = (("cli", cli), ("env", _environment_values(env)), ("config", file_values))
    origins = {"cli": "the command line", "config": f"the configuration file {file_name}"}

    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    for key, parse in _PARSERS.items():
        for source, layer in layers:
            if key in layer:
                origin = origins.get(source) or f"the variable {ENV_PREFIX}{key.upper()}"
                values[key] = _parse(key, parse, layer[key], origin)
                sources[key] = source
                break
        else:
            values[key] = _DEFAULTS[key]
            sources[key] = "default"

    _reconcile_ai_flags(values, sources)
    _reconcile_ai_budget(values, sources)
    # OLLAMA_HOST belongs to another program: it is only looked at when the AI is going to be used.
    if not values["no_ai"] and sources["ollama_url"] == "default" and env.get("OLLAMA_HOST"):
        try:
            values["ollama_url"] = normalize_ollama_host(env["OLLAMA_HOST"])
        except ValueError as error:
            raise ConfigError(str(error)) from None
        sources["ollama_url"] = "OLLAMA_HOST"
    if not values["no_ai"] and not values["allow_remote_ollama"] and not ollama_url_is_local(values["ollama_url"]):
        raise ConfigError(
            f"the Ollama URL {values['ollama_url']} is not local: "
            "pass --allow-remote-ollama (or set allow_remote_ollama = true in the configuration)"
        )

    workdir = values["workdir"]
    log_file = values["log_file"]
    values["log_file"] = workdir / "run.log" if log_file is None else workdir / log_file
    return Settings(**values, sources=sources)


def _parse(key: str, parse: Callable[[object], object], raw: object, origin: str) -> object:
    try:
        return parse(raw)
    except ValueError as error:
        raise ConfigError(f"invalid value for {key} in {origin}: {error}") from None


def _environment_values(env: Mapping[str, str]) -> dict[str, str]:
    return {key: env[name] for key in _PARSERS if env.get(name := ENV_PREFIX + key.upper())}


def _load_config_file(cli_path: object, env: Mapping[str, str]) -> tuple[dict[str, object], str]:
    path = cli_path or env.get(CONFIG_ENV)
    if not path:
        return {}, ""
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {path}") from None
    except OSError as error:
        raise ConfigError(f"cannot read the configuration file {path}: {error.strerror}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid configuration file {path}: {error}") from None
    unknown = sorted(set(data) - set(_PARSERS))
    if unknown:
        raise ConfigError(f"unknown option(s) in the configuration file {path}: {', '.join(unknown)}")
    return data, str(path)


def _reconcile_ai_budget(values: dict[str, object], sources: dict[str, str]) -> None:
    """A fixed number of frames and a time budget exclude each other; the higher layer wins."""
    if values["ai_frames"] is None or values["ai_time_budget"] is None:
        return
    frames_source, budget_source = sources["ai_frames"], sources["ai_time_budget"]
    if frames_source == budget_source:
        where = "on the command line" if frames_source == "cli" else f"in the same {frames_source} layer"
        raise ConfigError(f"--ai-frames and --ai-time-budget are mutually exclusive (both set {where})")
    loser = "ai_time_budget" if _SOURCE_RANK[frames_source] < _SOURCE_RANK[budget_source] else "ai_frames"
    values[loser] = None


def _reconcile_ai_flags(values: dict[str, object], sources: dict[str, str]) -> None:
    if not (values["require_ai"] and values["no_ai"]):
        return
    require_source, no_ai_source = sources["require_ai"], sources["no_ai"]
    if require_source == no_ai_source:
        where = "on the command line" if require_source == "cli" else f"in the same {require_source} layer"
        raise ConfigError(f"--require-ai and --no-ai are contradictory (both set {where})")
    if _SOURCE_RANK[require_source] < _SOURCE_RANK[no_ai_source]:
        values["no_ai"] = False
    else:
        values["require_ai"] = False
